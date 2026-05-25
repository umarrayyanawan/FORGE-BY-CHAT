"""Architecture Planner — Phase 4 of the FORGE planning pipeline.

Generates infrastructure plans, scalability profiles, security profiles,
architecture decision records (ADRs), and Mermaid.js diagrams from a
ProjectSpec and technology stack.

Usage::

    planner = ArchitecturePlanner(llm_client=get_llm_client())
    infra    = await planner.plan_infra(spec, stack)
    scale    = await planner.assess_scalability(spec, stack)
    security = await planner.generate_security_profile(spec)
    adrs     = await planner.generate_adr(decisions, stack)
    diagram  = await planner.generate_mermaid_diagram(topology)
"""

from __future__ import annotations

import re
from typing import Any

from system.core.planning.schemas import (
    InfrastructurePlan,
    RepoTopology,
    ScalabilityProfile,
    SecurityProfile,
)
from system.core.specification.schemas import ProjectSpec
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Cloud service mappings per provider
# ---------------------------------------------------------------------------

_AWS_SERVICES: dict[str, str] = {
    "database": "RDS (PostgreSQL)",
    "cache": "ElastiCache (Redis)",
    "queue": "SQS / ElastiMQ",
    "storage": "S3",
    "container": "EKS",
    "serverless": "Lambda",
    "cdn": "CloudFront",
    "lb": "ALB",
    "dns": "Route 53",
    "secrets": "Secrets Manager",
    "monitoring": "CloudWatch",
    "registry": "ECR",
}

_GCP_SERVICES: dict[str, str] = {
    "database": "Cloud SQL (PostgreSQL)",
    "cache": "Memorystore (Redis)",
    "queue": "Pub/Sub",
    "storage": "Cloud Storage",
    "container": "GKE",
    "serverless": "Cloud Run",
    "cdn": "Cloud CDN",
    "lb": "Cloud Load Balancing",
    "dns": "Cloud DNS",
    "secrets": "Secret Manager",
    "monitoring": "Cloud Monitoring",
    "registry": "Artifact Registry",
}

_AZURE_SERVICES: dict[str, str] = {
    "database": "Azure Database for PostgreSQL",
    "cache": "Azure Cache for Redis",
    "queue": "Azure Service Bus",
    "storage": "Azure Blob Storage",
    "container": "AKS",
    "serverless": "Azure Functions",
    "cdn": "Azure CDN",
    "lb": "Azure Load Balancer",
    "dns": "Azure DNS",
    "secrets": "Azure Key Vault",
    "monitoring": "Azure Monitor",
    "registry": "Azure Container Registry",
}

_SELF_HOSTED_SERVICES: dict[str, str] = {
    "database": "PostgreSQL (self-hosted)",
    "cache": "Redis (self-hosted)",
    "queue": "RabbitMQ / Redis",
    "storage": "MinIO",
    "container": "Kubernetes / Docker Swarm",
    "cdn": "NGINX",
    "lb": "NGINX / HAProxy",
    "secrets": "HashiCorp Vault",
    "monitoring": "Prometheus + Grafana",
    "registry": "Harbor / Docker Registry",
}

_PROVIDER_MAP: dict[str, dict[str, str]] = {
    "aws": _AWS_SERVICES,
    "gcp": _GCP_SERVICES,
    "azure": _AZURE_SERVICES,
    "self-hosted": _SELF_HOSTED_SERVICES,
    "docker": _SELF_HOSTED_SERVICES,
    "kubernetes": _SELF_HOSTED_SERVICES,
    "vercel": _AWS_SERVICES,  # Vercel typically backs onto AWS
    "railway": _AWS_SERVICES,
}

# Monthly cost estimates (USD) per service class, per provider
_COST_ESTIMATES: dict[str, dict[str, float]] = {
    "aws": {
        "RDS (PostgreSQL)": 150.0,
        "ElastiCache (Redis)": 80.0,
        "EKS": 300.0,
        "ALB": 25.0,
        "CloudFront": 20.0,
        "S3": 15.0,
        "Secrets Manager": 5.0,
        "CloudWatch": 20.0,
        "ECR": 5.0,
        "SQS / ElastiMQ": 10.0,
        "Route 53": 5.0,
    },
    "gcp": {
        "Cloud SQL (PostgreSQL)": 130.0,
        "Memorystore (Redis)": 70.0,
        "GKE": 280.0,
        "Cloud Load Balancing": 20.0,
        "Cloud CDN": 15.0,
        "Cloud Storage": 10.0,
        "Secret Manager": 3.0,
        "Cloud Monitoring": 15.0,
        "Artifact Registry": 5.0,
        "Pub/Sub": 8.0,
        "Cloud DNS": 3.0,
    },
}


class ArchitecturePlanner:
    """Generates all architecture sub-documents for a FORGE project."""

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client

    # ------------------------------------------------------------------
    # Infrastructure Plan
    # ------------------------------------------------------------------

    async def plan_infra(self, spec: ProjectSpec, stack: dict[str, str]) -> InfrastructurePlan:
        """Generate a cloud infrastructure plan from spec and stack.

        Determines cloud provider from spec.intent.deployment_target,
        selects required managed services, and estimates monthly cost.
        """
        # Resolve provider
        deploy_target = str(spec.intent.deployment_target).lower()
        provider_key = deploy_target if deploy_target in _PROVIDER_MAP else "aws"
        services_map = _PROVIDER_MAP[provider_key]

        # Always required
        required_services: list[str] = [
            services_map["database"],
            services_map["cache"],
            services_map["lb"],
            services_map["secrets"],
            services_map["monitoring"],
            services_map["registry"],
        ]

        # Conditional additions
        features_text = " ".join(spec.intent.core_features).lower()
        intent_text = spec.intent.raw_prompt.lower()
        combined = features_text + " " + intent_text

        if any(kw in combined for kw in ["file", "upload", "image", "video", "storage"]):
            required_services.append(services_map["storage"])

        if any(kw in combined for kw in ["cdn", "global", "static", "asset"]):
            required_services.append(services_map["cdn"])

        if any(kw in combined for kw in ["queue", "async", "background", "job", "worker"]):
            required_services.append(services_map["queue"])

        # Kubernetes / container orchestration
        scale_text = (spec.intent.scale_requirements or "").lower()
        needs_k8s = any(
            kw in scale_text or kw in combined
            for kw in ["kubernetes", "k8s", "scale", "million", "enterprise", "distributed"]
        )
        if needs_k8s or deploy_target == "kubernetes":
            required_services.append(services_map["container"])

        # Deduplicate while preserving order
        seen: set = set()
        unique_services: list[str] = []
        for svc in required_services:
            if svc not in seen:
                seen.add(svc)
                unique_services.append(svc)

        # Cost estimation
        cost_table = _COST_ESTIMATES.get(provider_key, _COST_ESTIMATES["aws"])
        cost_breakdown: dict[str, float] = {}
        total_cost = 0.0
        for svc in unique_services:
            cost = cost_table.get(svc, 20.0)  # default $20 for unknown
            cost_breakdown[svc] = cost
            total_cost += cost

        # HA check
        is_ha = any(
            kw in scale_text or kw in combined
            for kw in ["high availability", "ha", "multi-az", "multi-region", "99.9", "99.99"]
        )

        regions = ["us-east-1"] if provider_key == "aws" else ["us-central1"]
        if is_ha:
            if provider_key == "aws":
                regions = ["us-east-1", "us-west-2"]
            elif provider_key == "gcp":
                regions = ["us-central1", "europe-west1"]

        notes: list[str] = []
        if is_ha:
            notes.append("Multi-AZ deployment configured for high availability.")
        if total_cost > 1000:
            notes.append(
                f"Estimated monthly cost ${total_cost:.0f} — consider Reserved Instances for 30-40% savings."
            )

        infra = InfrastructurePlan(
            cloud_provider=provider_key,
            cloud_services=unique_services,
            estimated_monthly_cost_usd=round(total_cost, 2),
            cost_breakdown=cost_breakdown,
            regions=regions,
            high_availability=is_ha,
            disaster_recovery="warm-standby" if is_ha else "none",
            notes=notes,
        )

        logger.info(
            "Infrastructure plan generated",
            provider=provider_key,
            services=len(unique_services),
            estimated_cost=total_cost,
        )
        return infra

    # ------------------------------------------------------------------
    # Scalability Profile
    # ------------------------------------------------------------------

    async def assess_scalability(
        self, spec: ProjectSpec, stack: dict[str, str]
    ) -> ScalabilityProfile:
        """Parse scale requirements and return a ScalabilityProfile."""
        scale_text = (spec.intent.scale_requirements or "").lower()
        features_text = " ".join(spec.intent.core_features).lower()

        # Estimate RPS from scale requirements text
        rps = 0
        if "million" in scale_text:
            rps = 10_000
        elif "100k" in scale_text or "100,000" in scale_text:
            rps = 1_000
        elif "10k" in scale_text or "10,000" in scale_text:
            rps = 100
        elif "1k" in scale_text or "1,000" in scale_text:
            rps = 10
        else:
            rps = 50  # default reasonable assumption

        # Data volume estimate
        data_gb = 0.0
        if "petabyte" in scale_text or "pb" in scale_text:
            data_gb = 1_000_000.0
        elif "terabyte" in scale_text or "tb" in scale_text:
            data_gb = 1_000.0
        elif "gigabyte" in scale_text or "gb" in scale_text:
            data_gb = 100.0
        else:
            data_gb = 10.0

        # Identify bottlenecks
        bottlenecks: list[str] = []
        recommendations: list[str] = []

        if rps > 1000:
            bottlenecks.append("Database connection pool saturation at high RPS")
            recommendations.append("Use PgBouncer connection pooler in front of PostgreSQL")

        if "session" in features_text or "auth" in features_text:
            if "redis" not in stack.get("cache", "").lower():
                bottlenecks.append("In-memory session storage will not scale horizontally")
                recommendations.append("Move session storage to Redis")

        if rps > 500 and "cdn" not in stack.get("cdn", "").lower():
            bottlenecks.append("Static asset delivery without CDN will increase origin load")
            recommendations.append("Add CDN (CloudFront / Cloudflare) for static assets")

        if "million" in scale_text and "read replica" not in stack.get("database", "").lower():
            bottlenecks.append("Single database write node cannot sustain million-user read load")
            recommendations.append("Add PostgreSQL read replicas for read-heavy workloads")

        if not bottlenecks:
            recommendations.append(
                "Architecture is well-suited for projected scale — monitor metrics post-launch."
            )

        # Caching strategy
        cache_tech = stack.get("cache", "").lower()
        if "redis" in cache_tech and rps > 500:
            caching = "multi-layer"
        elif "redis" in cache_tech:
            caching = "redis"
        elif "cdn" in stack:
            caching = "cdn"
        else:
            caching = "none"

        # Database scaling
        db_val = stack.get("database", "")
        if "read replica" in db_val.lower():
            db_scaling = "read-replicas"
        elif "sharding" in scale_text or "million" in scale_text:
            db_scaling = "read-replicas"
            recommendations.append(
                "Consider CQRS pattern for extreme read/write split requirements"
            )
        else:
            db_scaling = "vertical"

        profile = ScalabilityProfile(
            expected_users=spec.intent.scale_requirements or "Not specified",
            requests_per_second=rps,
            data_volume_gb=data_gb,
            bottlenecks=bottlenecks,
            recommendations=recommendations,
            horizontal_scaling=rps > 100,
            caching_strategy=caching,
            database_scaling=db_scaling,
        )

        logger.info(
            "Scalability profile assessed",
            rps=rps,
            bottleneck_count=len(bottlenecks),
        )
        return profile

    # ------------------------------------------------------------------
    # Security Profile
    # ------------------------------------------------------------------

    async def generate_security_profile(self, spec: ProjectSpec) -> SecurityProfile:
        """Generate a SecurityProfile from spec intent security requirements.

        JWT auth and HTTPS are always enabled.
        Compliance requirements are parsed from spec.intent.security_requirements.
        """
        sec_reqs = [r.lower() for r in spec.intent.security_requirements]
        features_text = " ".join(spec.intent.core_features).lower()
        combined = " ".join(sec_reqs) + " " + features_text

        # Auth method
        if any(kw in combined for kw in ["oauth", "sso", "openid", "google login", "github login"]):
            auth_method = "OAuth2 + JWT"
        elif any(kw in combined for kw in ["api key", "api_key", "machine-to-machine", "m2m"]):
            auth_method = "JWT + API Key"
        elif any(kw in combined for kw in ["mtls", "certificate", "mutual tls"]):
            auth_method = "mTLS"
        else:
            auth_method = "JWT"

        # Compliance
        compliance: list[str] = []
        compliance_checks = {
            "gdpr": "GDPR",
            "hipaa": "HIPAA",
            "soc2": "SOC 2",
            "soc 2": "SOC 2",
            "pci": "PCI DSS",
            "iso 27001": "ISO 27001",
            "iso27001": "ISO 27001",
            "fedramp": "FedRAMP",
        }
        for keyword, label in compliance_checks.items():
            if keyword in combined and label not in compliance:
                compliance.append(label)

        # Additional controls
        additional: list[str] = []
        if any(kw in combined for kw in ["mfa", "2fa", "two-factor", "multi-factor"]):
            additional.append("Multi-factor authentication (MFA/TOTP)")
        if any(kw in combined for kw in ["waf", "web application firewall"]):
            additional.append("Web Application Firewall (WAF)")
        if any(kw in combined for kw in ["ddos", "rate limit"]):
            additional.append("DDoS protection + rate limiting")
        if any(kw in combined for kw in ["audit", "log", "trail"]):
            additional.append("Audit logging with tamper-evident log storage")
        if any(kw in combined for kw in ["encrypt", "at rest", "at-rest"]):
            additional.append("Encryption at rest (AES-256)")
        if "HIPAA" in compliance or "PCI DSS" in compliance:
            additional.append("Dedicated key management (KMS)")
            additional.append("Regular vulnerability scanning")

        profile = SecurityProfile(
            auth_method=auth_method,
            https_enforced=True,
            rate_limiting=True,
            input_validation=True,
            sql_injection_protection=True,
            xss_protection=True,
            csrf_protection=True,
            cors_configured=True,
            secrets_in_env=True,
            compliance=compliance,
            additional_controls=additional,
            vulnerability_scanning="soc" in combined or "iso" in combined,
            penetration_testing="pentest" in combined or "penetration" in combined,
        )

        logger.info(
            "Security profile generated",
            auth=auth_method,
            compliance=compliance,
        )
        return profile

    # ------------------------------------------------------------------
    # Architecture Decision Records
    # ------------------------------------------------------------------

    async def generate_adr(
        self, decisions: list[str], stack: dict[str, str]
    ) -> list[dict[str, str]]:
        """Generate one ADR per major stack choice.

        Returns a list of dicts with keys:
            title, context, decision, rationale, status
        """
        adrs: list[dict[str, str]] = []

        # ADR per stack layer
        adr_templates = [
            {
                "layer": "backend",
                "title": f"ADR-001: Backend Framework — {stack.get('backend', 'FastAPI')}",
                "context": (
                    "The project requires a high-performance, async-capable REST API framework "
                    "that supports dependency injection, auto-generated OpenAPI docs, and "
                    "integrates well with SQLAlchemy and Pydantic."
                ),
                "decision": f"Use {stack.get('backend', 'FastAPI')} as the primary backend framework.",
                "rationale": (
                    f"{stack.get('backend', 'FastAPI')} provides native async/await support, "
                    "automatic request validation via Pydantic, OpenAPI documentation generation, "
                    "high throughput benchmarks, and a large ecosystem of middleware. "
                    "It enables the team to ship quickly while maintaining correctness."
                ),
                "status": "Accepted",
            },
            {
                "layer": "frontend",
                "title": f"ADR-002: Frontend Framework — {stack.get('frontend', 'Next.js')}",
                "context": (
                    "The project requires a modern frontend framework supporting SSR/SSG, "
                    "TypeScript, and seamless API integration."
                ),
                "decision": f"Use {stack.get('frontend', 'Next.js')} as the frontend framework.",
                "rationale": (
                    f"{stack.get('frontend', 'Next.js')} supports server-side rendering for SEO, "
                    "static site generation for performance, TypeScript out of the box, "
                    "and the App Router for nested layouts. Vercel deployment is trivial."
                ),
                "status": "Accepted",
            },
            {
                "layer": "database",
                "title": f"ADR-003: Primary Database — {stack.get('database', 'PostgreSQL')}",
                "context": (
                    "The project requires a reliable, ACID-compliant relational database with "
                    "support for complex queries, JSON fields, and full-text search."
                ),
                "decision": f"Use {stack.get('database', 'PostgreSQL')} as the primary database.",
                "rationale": (
                    "PostgreSQL is the industry standard for production relational workloads, "
                    "offering JSONB support, PostGIS extensions, excellent query optimiser, "
                    "robust replication, and broad managed cloud provider support (RDS, Cloud SQL, Neon)."
                ),
                "status": "Accepted",
            },
            {
                "layer": "cache",
                "title": f"ADR-004: Caching Layer — {stack.get('cache', 'Redis')}",
                "context": (
                    "The API requires sub-millisecond response times for frequently read data "
                    "and a reliable pub/sub bus for real-time events."
                ),
                "decision": f"Use {stack.get('cache', 'Redis')} for caching and messaging.",
                "rationale": (
                    "Redis provides in-memory key-value storage with optional persistence, "
                    "pub/sub messaging, sorted sets for leaderboards, and is the de-facto "
                    "standard Celery broker. A single Redis cluster satisfies caching, "
                    "session storage, and background task queuing."
                ),
                "status": "Accepted",
            },
            {
                "layer": "infra",
                "title": f"ADR-005: Deployment Infrastructure — {stack.get('infra', 'Docker')}",
                "context": (
                    "The project needs a reproducible, environment-agnostic deployment pipeline "
                    "that scales from development to production."
                ),
                "decision": f"Deploy using {stack.get('infra', 'Docker')}.",
                "rationale": (
                    "Containerisation with Docker ensures environment parity across dev/test/prod. "
                    "Kubernetes (when required) provides declarative scaling, self-healing, "
                    "rolling deployments, and cloud-provider independence."
                ),
                "status": "Accepted",
            },
            {
                "layer": "auth",
                "title": f"ADR-006: Authentication Strategy — {stack.get('auth', 'JWT')}",
                "context": (
                    "The application must authenticate users securely without coupling to a "
                    "proprietary identity provider."
                ),
                "decision": f"Implement {stack.get('auth', 'JWT')} for authentication.",
                "rationale": (
                    "JWT (JSON Web Tokens) are stateless, verifiable without database lookup, "
                    "and carry structured claims. Combined with short expiry and refresh token "
                    "rotation, this provides a secure, scalable auth mechanism with no "
                    "additional infrastructure dependency."
                ),
                "status": "Accepted",
            },
        ]

        # Add any extra decisions passed in
        for i, decision_text in enumerate(decisions, start=len(adr_templates) + 1):
            adr_templates.append(
                {
                    "layer": "custom",
                    "title": f"ADR-{i:03d}: {decision_text[:60]}",
                    "context": "Custom architectural decision identified during planning.",
                    "decision": decision_text,
                    "rationale": "Evaluated during the architecture planning phase.",
                    "status": "Proposed",
                }
            )

        # Build final ADR list — filter out layers where tech is "None"
        for template in adr_templates:
            layer = template.pop("layer")
            tech_value = stack.get(layer, "")
            if tech_value and "none" not in tech_value.lower():
                adrs.append(template)

        logger.info("ADRs generated", count=len(adrs))
        return adrs

    # ------------------------------------------------------------------
    # Mermaid Diagram
    # ------------------------------------------------------------------

    async def generate_mermaid_diagram(self, topology: RepoTopology) -> str:
        """Generate a Mermaid.js graph TD diagram for all services and connections.

        Returns a complete mermaid diagram string ready to embed in markdown.
        """
        lines: list[str] = ["graph TD"]

        # Style definitions
        lines.append("    classDef backend fill:#dbeafe,stroke:#2563eb,color:#1e3a5f")
        lines.append("    classDef frontend fill:#dcfce7,stroke:#16a34a,color:#14532d")
        lines.append("    classDef database fill:#fef9c3,stroke:#ca8a04,color:#713f12")
        lines.append("    classDef cache fill:#fce7f3,stroke:#db2777,color:#831843")
        lines.append("    classDef worker fill:#ede9fe,stroke:#7c3aed,color:#3b0764")
        lines.append("    classDef gateway fill:#ffedd5,stroke:#ea580c,color:#7c2d12")
        lines.append("")

        # Build node definitions
        node_ids: dict[str, str] = {}
        type_class_map = {
            "backend": "backend",
            "frontend": "frontend",
            "database": "database",
            "cache": "cache",
            "worker": "worker",
            "gateway": "gateway",
        }

        for svc in topology.services:
            # Create a safe node ID (no spaces, no special chars)
            node_id = re.sub(r"[^a-zA-Z0-9]", "_", svc.name).upper()
            node_ids[svc.name] = node_id
            label = f"{svc.name}\\n[{svc.technology}]"
            if svc.port and svc.port > 0:
                label += f"\\n:{svc.port}"
            lines.append(f'    {node_id}["{label}"]')

        lines.append("")

        # Add edges based on dependencies
        for svc in topology.services:
            src_id = node_ids.get(svc.name, "")
            if not src_id:
                continue
            for dep_name in svc.dependencies:
                dep_id = node_ids.get(dep_name, "")
                if dep_id:
                    lines.append(f"    {src_id} --> {dep_id}")

        lines.append("")

        # Apply class styles
        for svc in topology.services:
            node_id = node_ids.get(svc.name, "")
            css_class = type_class_map.get(svc.service_type, "backend")
            if node_id:
                lines.append(f"    class {node_id} {css_class}")

        diagram = "\n".join(lines)
        logger.info(
            "Mermaid diagram generated",
            service_count=len(topology.services),
            line_count=len(lines),
        )
        return diagram
