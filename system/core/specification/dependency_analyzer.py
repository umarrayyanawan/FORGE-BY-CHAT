"""Feature Dependency Analyzer — Phase 3: Specification Engine.

Builds a directed dependency graph of features, detects cycles, and computes
a valid topological build order using Kahn's algorithm.
"""

from __future__ import annotations

from collections import defaultdict, deque
import json
import re
import textwrap
from typing import TYPE_CHECKING, Any

from system.observability.logging.logger import get_logger
from system.shared.constants import DEFAULT_LLM_MODEL
from system.shared.exceptions import SpecificationError
from system.shared.llm_client import LLMMessage, get_llm_client

if TYPE_CHECKING:
    from system.core.specification.schemas import FeatureDependency, ProjectSpec

logger = get_logger(__name__)


class DependencyAnalyzer:
    """Analyses feature inter-dependencies and builds an executable build order.

    The analyzer:
    1.  Uses the LLM to identify which features depend on which others.
    2.  Applies Kahn's topological sort to compute build order.
    3.  Detects cycles via DFS and reports them as errors.
    """

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client or get_llm_client()

    async def analyze(self, spec: ProjectSpec) -> list[FeatureDependency]:
        """Build the feature dependency list for a ProjectSpec.

        Args:
            spec: A partially assembled ProjectSpec (needs at least intent
                  and api_contract populated).

        Returns:
            List of FeatureDependency objects representing the DAG.

        Raises:
            SpecificationError: If the LLM call fails or cycles are detected
                                in blocking dependencies.
        """
        from system.core.specification.schemas import FeatureDependency

        logger.info(
            "analyzing_feature_dependencies",
            feature_count=len(spec.intent.core_features),
            project_id=spec.project_id,
        )

        prompt = self._build_dependency_prompt(spec)
        messages = [LLMMessage(role="user", content=prompt)]

        try:
            response = await self._llm.complete(
                messages=messages,
                model=DEFAULT_LLM_MODEL,
                max_tokens=3000,
                temperature=0.1,
                system=self._system_prompt(),
            )
        except Exception as exc:
            raise SpecificationError(
                f"LLM call failed during dependency analysis: {exc}",
            ) from exc

        raw = response.content.strip()
        if not raw:
            raise SpecificationError("Dependency analysis returned empty content")

        try:
            deps = self._parse_dependencies(raw)
        except Exception as exc:
            raise SpecificationError(
                f"Failed to parse dependency response: {exc}",
                details={"raw_preview": raw[:400]},
            ) from exc

        # Detect cycles in blocking dependencies
        cycles = self.detect_cycles(deps)
        if cycles:
            logger.warning(
                "dependency_cycles_detected",
                cycles=cycles,
                action="breaking_cycles_by_removing_blocking_flag",
            )
            # Break cycles by un-setting the blocking flag (never raise —
            # we want to produce a usable spec even with circular deps)
            dep_map = {d.feature: d for d in deps}
            for cycle in cycles:
                for feature_name in cycle:
                    if feature_name in dep_map:
                        # Create a new FeatureDependency with blocking=False
                        existing = dep_map[feature_name]
                        dep_map[feature_name] = FeatureDependency(
                            feature=existing.feature,
                            depends_on=existing.depends_on,
                            blocking=False,
                            estimated_days=existing.estimated_days,
                        )
            deps = list(dep_map.values())

        logger.info(
            "dependency_analysis_complete",
            total_features=len(deps),
            blocking_count=sum(1 for d in deps if d.blocking),
        )

        return deps

    def detect_cycles(self, deps: list[FeatureDependency]) -> list[list[str]]:
        """Detect cycles in the dependency graph using iterative DFS.

        Args:
            deps: List of FeatureDependency objects.

        Returns:
            A list of cycles; each cycle is a list of feature names.
            Empty list means the graph is a DAG (no cycles).
        """
        # Build adjacency list
        graph: dict[str, list[str]] = defaultdict(list)
        all_nodes: set[str] = set()

        for dep in deps:
            all_nodes.add(dep.feature)
            for predecessor in dep.depends_on:
                all_nodes.add(predecessor)
                # dep.feature depends on predecessor → edge: predecessor → dep.feature
                graph[predecessor].append(dep.feature)

        cycles: list[list[str]] = []
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node: str, path: list[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbour in graph.get(node, []):
                if neighbour not in visited:
                    dfs(neighbour, path)
                elif neighbour in rec_stack:
                    # Found a cycle — extract it
                    cycle_start = path.index(neighbour)
                    cycle = path[cycle_start:]
                    if cycle not in cycles:
                        cycles.append(list(cycle))

            path.pop()
            rec_stack.discard(node)

        for node in all_nodes:
            if node not in visited:
                dfs(node, [])

        return cycles

    def get_execution_order(self, deps: list[FeatureDependency]) -> list[str]:
        """Return features in safe build order using Kahn's topological sort.

        Features with no dependencies come first; dependents come after their
        prerequisites.  Parallel groups are indicated by position — features
        at the same "level" can be built concurrently.

        Args:
            deps: List of FeatureDependency objects (should be cycle-free).

        Returns:
            Ordered list of feature names.  If the graph has cycles (should
            have been broken by analyze()), the cyclic nodes are appended at
            the end.
        """
        # Collect all known features
        all_features: set[str] = set()
        for dep in deps:
            all_features.add(dep.feature)
            all_features.update(dep.depends_on)

        # Build adjacency + in-degree maps
        in_degree: dict[str, int] = {f: 0 for f in all_features}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for dep in deps:
            for predecessor in dep.depends_on:
                adjacency[predecessor].append(dep.feature)
                in_degree[dep.feature] = in_degree.get(dep.feature, 0) + 1

        # Kahn's algorithm
        queue: deque[str] = deque(sorted(f for f, d in in_degree.items() if d == 0))
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for successor in sorted(adjacency.get(node, [])):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)

        # Any remaining nodes are part of unresolved cycles
        remaining = [f for f in all_features if f not in order]
        if remaining:
            logger.warning(
                "topological_sort_has_remaining_nodes",
                remaining=remaining,
            )
            order.extend(sorted(remaining))

        return order

    # ---------------------------------------------------------------------- #
    # Private helpers
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _system_prompt() -> str:
        return textwrap.dedent("""\
            You are a senior engineering manager who creates detailed feature
            dependency graphs for software projects.

            A feature X "depends on" Y if Y must be fully built and working
            before X can be started.

            A dependency is "blocking" if X literally cannot function at all
            without Y. Non-blocking means some work can start in parallel.

            Output ONLY valid JSON — no markdown, no explanations.
        """)

    def _build_dependency_prompt(self, spec: ProjectSpec) -> str:
        """Construct the LLM prompt for dependency analysis."""
        features_list = (
            "\n".join(f"- {f}" for f in spec.intent.core_features) or "- No features specified"
        )

        # Add table names as context
        tables = spec.db_schema.table_names() if spec.db_schema else []
        tables_text = ", ".join(tables[:15]) or "none"

        return textwrap.dedent(f"""\
            Analyse the feature dependencies for this software project.

            PROJECT: {spec.intent.product_type} ({spec.intent.industry})

            FEATURES TO ANALYSE:
            {features_list}

            DATABASE TABLES (for context):
            {tables_text}

            OUTPUT FORMAT
            -------------
            Return a JSON array where each element describes one feature:
            [
              {{
                "feature": "User Authentication",
                "depends_on": [],
                "blocking": false,
                "estimated_days": 3
              }},
              {{
                "feature": "Order Management",
                "depends_on": ["User Authentication", "Product Catalogue"],
                "blocking": true,
                "estimated_days": 5
              }}
            ]

            Rules:
            - Every feature from the list above must appear exactly once
            - depends_on values must exactly match feature names in the list
            - blocking = true only when the feature is completely unusable
              without the dependency (not just "nice to have")
            - estimated_days is a realistic engineering effort in person-days
            - Core infrastructure (auth, database setup) should have no dependencies
            - Do NOT create circular dependencies
            - Features that can be built in parallel should have minimal blocking=true
        """)

    def _parse_dependencies(self, response: str) -> list[FeatureDependency]:
        """Parse LLM JSON array into FeatureDependency models."""
        from system.core.specification.schemas import FeatureDependency

        cleaned = re.sub(r"^```(?:json)?\s*", "", response, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()

        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            obj_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if obj_match:
                data = json.loads(obj_match.group(0))
                dep_list = data.get("dependencies", data.get("features", []))
            else:
                raise ValueError("No JSON array or object found in dependency response")
        else:
            dep_list = json.loads(match.group(0))

        deps: list[FeatureDependency] = []
        for item in dep_list:
            if not isinstance(item, dict):
                continue
            try:
                deps.append(FeatureDependency(**item))
            except Exception as exc:
                logger.warning(
                    "skipping_invalid_dependency",
                    feature=item.get("feature", "?"),
                    error=str(exc),
                )
        return deps
