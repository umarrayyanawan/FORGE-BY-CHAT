"""Scalability Validator — Phase 4 of the FORGE planning pipeline.

Validates an ArchitecturePlan against scalability best practices and the
project specification's scale requirements.  Returns a ValidationResult
with a boolean flag, a list of blocking issues, and a list of improvement
recommendations.

Usage::

    validator = ScalabilityValidator()
    result = validator.validate(plan, spec)
    if not result.is_valid:
        for issue in result.issues:
            print(f"[ISSUE] {issue}")
    for rec in result.recommendations:
        print(f"[REC]   {rec}")
"""

from __future__ import annotations

from dataclasses import dataclass, field

from system.core.planning.schemas import (
    ArchitecturePlan,
    RepoTopology,
)
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of a scalability validation run."""

    is_valid: bool
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def add_issue(self, msg: str) -> None:
        self.issues.append(msg)
        self.is_valid = False

    def add_recommendation(self, msg: str) -> None:
        self.recommendations.append(msg)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class ScalabilityValidator:
    """Validates an ArchitecturePlan for production-grade scalability."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self, plan: ArchitecturePlan, spec: ProjectSpec) -> ValidationResult:
        """Run all scalability checks and return a ValidationResult.

        Checks performed:
        1. Single points of failure in topology.
        2. Database read-scaling sufficiency.
        3. Stateless service design.
        4. Caching strategy adequacy.

        ``is_valid`` is True only when **no** blocking issues are found.
        Recommendations are advisory and do not affect validity.
        """
        result = ValidationResult(is_valid=True)

        # Build a lightweight RepoTopology from the plan's services
        topology = _plan_to_topology(plan)

        # Run each check
        spof_issues = self.check_single_points_of_failure(topology)
        for issue in spof_issues:
            result.add_issue(issue)

        db_issues = self.check_database_scaling(spec, plan)
        for issue in db_issues:
            result.add_issue(issue)

        stateless_issues = self.check_stateless_services(topology)
        for issue in stateless_issues:
            result.add_issue(issue)

        cache_warnings = self.check_caching_strategy(plan)
        for warning in cache_warnings:
            # Caching warnings are advisory — don't fail validation
            result.add_recommendation(warning)

        # General recommendations
        if not result.issues:
            result.add_recommendation(
                "Architecture passes all scalability checks. "
                "Monitor key metrics (p99 latency, error rate, DB connections) post-launch."
            )

        logger.info(
            "Scalability validation complete",
            is_valid=result.is_valid,
            issue_count=len(result.issues),
            recommendation_count=len(result.recommendations),
        )
        return result

    # ------------------------------------------------------------------
    # Check: single points of failure
    # ------------------------------------------------------------------

    def check_single_points_of_failure(self, topology: RepoTopology) -> list[str]:
        """Flag any service with replicas=1 that has no failover configured.

        A service is considered a SPOF when:
        - min_replicas == 1 (or unset)  AND
        - it is not a database / cache type (those have dedicated HA patterns)  AND
        - no backup / standby is declared

        Returns a list of human-readable issue strings.
        """
        issues: list[str] = []
        for svc in topology.services:
            scaling = svc.scaling or {}
            min_replicas = scaling.get("min_replicas", 1)
            # Databases and caches have their own HA mechanisms
            if svc.service_type in {"database", "cache"}:
                continue
            # Port == 0 means queue consumer — less critical for SPOF
            if svc.port == 0:
                continue
            if min_replicas < 2:
                issues.append(
                    f"Service '{svc.name}' ({svc.service_type}) has min_replicas=1 "
                    "and no failover — this is a single point of failure. "
                    "Set min_replicas >= 2 or add a hot-standby replica."
                )
        return issues

    # ------------------------------------------------------------------
    # Check: database scaling
    # ------------------------------------------------------------------

    def check_database_scaling(self, spec: ProjectSpec, plan: ArchitecturePlan) -> list[str]:
        """Warn when no read replicas are configured but scale is high.

        Issues a blocking issue when the spec mentions 'million' users / records
        and the database component / service has no read replica configuration.
        """
        issues: list[str] = []
        scale_text = (spec.intent.scale_requirements or "").lower()

        if "million" not in scale_text and "10m" not in scale_text:
            return issues  # scale is not extreme — no issue

        # Check infra_components for read replica mention
        db_components = [c for c in plan.infra_components if c.component_type == "database"]
        has_read_replica = any(
            "replica" in c.config.get("notes", "").lower() or c.config.get("read_replicas", 0) > 0
            for c in db_components
        )

        # Also check services
        db_services = [s for s in plan.services if s.service_type == "database"]
        has_replica_in_service = any(
            "replica" in s.description.lower() or s.scaling.get("max_replicas", 1) > 1
            for s in db_services
        )

        if not has_read_replica and not has_replica_in_service:
            issues.append(
                "Scale requirements mention millions of users/records, but no read replicas "
                "are configured for the database. Add at least one PostgreSQL read replica "
                "to distribute read load and prevent write-path saturation."
            )

        return issues

    # ------------------------------------------------------------------
    # Check: stateless services
    # ------------------------------------------------------------------

    def check_stateless_services(self, topology: RepoTopology) -> list[str]:
        """Flag backend/worker services that appear to store session state in memory.

        Heuristics:
        - Service description mentions 'in-memory session' or 'local cache'.
        - Service scaling.max_replicas > 1 AND no external cache dependency.

        Returns issue strings for services that are not horizontally scalable.
        """
        issues: list[str] = []
        cache_names = {s.name for s in topology.services if s.service_type == "cache"}

        for svc in topology.services:
            if svc.service_type not in {"backend", "worker"}:
                continue

            desc_lower = svc.description.lower()
            has_local_state_hint = any(
                kw in desc_lower
                for kw in ["in-memory session", "local cache", "sticky session", "affinity"]
            )

            if has_local_state_hint:
                issues.append(
                    f"Service '{svc.name}' appears to store session/state in local memory "
                    "(detected from description). This prevents horizontal scaling. "
                    "Migrate session/state to Redis or another external store."
                )
                continue

            # Check: service scales to >1 but has no cache dependency
            max_replicas = svc.scaling.get("max_replicas", 1)
            has_cache_dep = any(dep in cache_names for dep in svc.dependencies)

            if max_replicas > 1 and not has_cache_dep and "session" in desc_lower:
                issues.append(
                    f"Service '{svc.name}' scales to {max_replicas} replicas but has no "
                    "dependency on an external cache/session store. Add Redis as a dependency "
                    "to ensure distributed session consistency."
                )

        return issues

    # ------------------------------------------------------------------
    # Check: caching strategy
    # ------------------------------------------------------------------

    def check_caching_strategy(self, plan: ArchitecturePlan) -> list[str]:
        """Warn when there is no Redis cache but the scale is high.

        Returns advisory warning strings (not blocking issues).
        """
        warnings: list[str] = []

        # Detect high-scale from infra components or services
        infra_names = [c.technology.lower() for c in plan.infra_components]
        service_techs = [s.technology.lower() for s in plan.services]
        all_techs = " ".join(infra_names + service_techs)

        has_redis = "redis" in all_techs or "elasticache" in all_techs or "memorystore" in all_techs
        has_cdn = "cdn" in all_techs or "cloudfront" in all_techs or "cloudflare" in all_techs

        # Check if scale indicators are present in any plan metadata
        is_high_scale = (
            any(
                "kubernetes" in t or "eks" in t or "gke" in t or "aks" in t
                for t in infra_names + service_techs
            )
            or len(plan.services) > 5
        )

        if is_high_scale and not has_redis:
            warnings.append(
                "High-scale architecture detected but no Redis cache is configured. "
                "Add Redis (ElastiCache / Memorystore) to reduce database load and "
                "enable session storage, rate limiting, and pub/sub messaging."
            )

        if is_high_scale and not has_cdn:
            warnings.append(
                "High-scale architecture detected but no CDN is configured. "
                "Add CloudFront or Cloudflare to cache static assets at the edge "
                "and reduce origin server load."
            )

        if not has_redis and len(plan.services) > 3:
            warnings.append(
                "Multi-service architecture without a shared cache may lead to redundant "
                "database queries across services. Consider adding Redis as a shared cache layer."
            )

        return warnings


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _plan_to_topology(plan: ArchitecturePlan) -> RepoTopology:
    """Build a minimal RepoTopology from an ArchitecturePlan for validation."""
    from system.core.planning.schemas import RepoTopology

    return RepoTopology(
        topology_id=plan.plan_id,
        project_id=plan.project_id,
        repo_type="monorepo",
        services=plan.services,
    )
