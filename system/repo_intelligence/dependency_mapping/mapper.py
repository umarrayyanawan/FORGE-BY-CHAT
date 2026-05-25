"""Dependency Mapper — walks a project tree and builds the dependency graph.

Uses the ASTParser to extract imports from every source file, then stores the
results in Neo4j via GraphBuilder.  Also provides topological sort (build order)
and impact analysis.
"""

from __future__ import annotations

from collections import defaultdict, deque
import os

from system.observability.logging.logger import get_logger
from system.repo_intelligence.ast.parser import ASTParser
from system.repo_intelligence.graph.builder import GraphBuilder
from system.repo_intelligence.schemas import CodeChunk, DependencyEdge, FileNode
from system.shared.exceptions import RepoIntelligenceError

logger = get_logger(__name__)

_SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}


class DependencyMapper:
    """Walk a project tree and build its dependency graph."""

    def __init__(self, ast_parser: ASTParser, graph_builder: GraphBuilder) -> None:
        self._parser = ast_parser
        self._graph = graph_builder

    # ------------------------------------------------------------------
    # Project-wide mapping
    # ------------------------------------------------------------------

    async def map_project(self, project_id: str, root_path: str) -> dict[str, list[str]]:
        """Walk all supported files under *root_path* and build the graph.

        Args:
            project_id: FORGE project identifier.
            root_path: Absolute path to the repository root.

        Returns:
            Dict mapping each file path to its list of resolved dependency paths.

        Raises:
            RepoIntelligenceError: If the root path does not exist.
        """
        if not os.path.isdir(root_path):
            raise RepoIntelligenceError(
                f"Root path does not exist: {root_path}",
                details={"root_path": root_path},
            )

        all_files = self._collect_files(root_path)
        logger.info(
            "DependencyMapper: mapping %d files for project %s",
            len(all_files),
            project_id,
        )

        dep_map: dict[str, list[str]] = {}
        all_chunks: list[CodeChunk] = []

        for file_path in all_files:
            try:
                chunks = self._parser.parse_file(file_path, project_id)
                all_chunks.extend(chunks)
                resolved: list[str] = []
                for chunk in chunks:
                    for raw_import in chunk.dependencies:
                        resolved_path = self._resolve_import_path(raw_import, file_path, root_path)
                        if resolved_path and resolved_path not in resolved:
                            resolved.append(resolved_path)
                dep_map[file_path] = resolved
            except RepoIntelligenceError as exc:
                logger.warning("Skipping %s: %s", file_path, exc.message)
                dep_map[file_path] = []

        # Build the Neo4j graph
        await self._graph.build_project_graph(project_id, all_chunks)

        # Create resolved file-level edges
        for from_file, deps in dep_map.items():
            for to_file in deps:
                edge = DependencyEdge(
                    from_file=from_file,
                    to_file=to_file,
                    dependency_type="import",
                    symbols=[],
                )
                try:
                    await self._graph.create_dependency_edge(project_id, edge)
                except Exception as exc:
                    logger.debug("Edge creation skipped: %s", exc)

        return dep_map

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    async def update_file(self, project_id: str, file_path: str) -> None:
        """Re-index a single file after it has changed.

        Removes existing chunk nodes for the file and re-parses.
        """
        self._guess_root(file_path)
        try:
            chunks = self._parser.parse_file(file_path, project_id)
        except RepoIntelligenceError as exc:
            logger.warning("Failed to re-parse %s: %s", file_path, exc.message)
            return

        # Re-upsert file node
        file_node = FileNode(
            path=file_path,
            language=chunks[0].language if chunks else "unknown",
            size_bytes=os.path.getsize(file_path),
            chunk_count=len(chunks),
            import_count=len(chunks[0].dependencies) if chunks else 0,
            export_count=len(chunks[0].exports) if chunks else 0,
            last_modified=__import__("datetime").datetime.utcnow(),
            project_id=project_id,
        )
        await self._graph.upsert_file_node(project_id, file_node)

        for chunk in chunks:
            await self._graph.upsert_chunk_node(project_id, chunk)
            await self._graph._create_contains_edge(project_id, chunk.file_path, chunk.chunk_id)

        logger.info("Updated index for %s (%d chunks)", file_path, len(chunks))

    # ------------------------------------------------------------------
    # Topological sort (build order)
    # ------------------------------------------------------------------

    async def get_build_order(self, project_id: str) -> list[str]:
        """Return files in dependency order (leaves first).

        Uses Kahn's algorithm on the graph stored in Neo4j.

        Returns:
            Ordered list of file paths (dependency-first).  Files in cycles
            are appended at the end.
        """
        # Fetch all edges from Neo4j
        records = await self._graph._db.run_query(
            """
            MATCH (a:ForgeFile {project_id: $pid})-[:IMPORTS]->(b:ForgeFile {project_id: $pid})
            WHERE NOT coalesce(a.external, false) AND NOT coalesce(b.external, false)
            RETURN a.path AS from_path, b.path AS to_path
            """,
            {"pid": project_id},
        )

        in_degree: dict[str, int] = defaultdict(int)
        adjacency: dict[str, list[str]] = defaultdict(list)
        all_nodes: set[str] = set()

        for r in records:
            f, t = r.get("from_path"), r.get("to_path")
            if f and t:
                adjacency[t].append(f)  # t must be built before f
                in_degree[f] += 1
                all_nodes.update([f, t])

        # All nodes with zero in-degree (no deps)
        queue: deque = deque([n for n in all_nodes if in_degree[n] == 0])
        order: list[str] = []
        visited: set[str] = set()

        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            order.append(node)
            for dependent in adjacency.get(node, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # Append remaining (cycle members)
        for node in all_nodes:
            if node not in visited:
                order.append(node)

        return order

    # ------------------------------------------------------------------
    # Circular dependency detection
    # ------------------------------------------------------------------

    async def detect_circular_deps(self, project_id: str) -> list[list[str]]:
        """Return all circular dependency chains found in the project graph."""
        return await self._graph.find_cycles(project_id)

    # ------------------------------------------------------------------
    # Impact analysis
    # ------------------------------------------------------------------

    async def get_affected_files(self, project_id: str, changed_files: list[str]) -> list[str]:
        """Return all files affected by changes to *changed_files*.

        Includes the changed files themselves plus anything that imports them
        (directly or transitively).

        Args:
            project_id: FORGE project identifier.
            changed_files: Absolute paths of files that changed.

        Returns:
            Deduplicated list of affected file paths.
        """
        affected: set[str] = set(changed_files)
        for fp in changed_files:
            try:
                impact = await self._graph.get_impact_set(project_id, fp)
                affected.update(impact)
            except Exception as exc:
                logger.debug("Impact set failed for %s: %s", fp, exc)
        return list(affected)

    # ------------------------------------------------------------------
    # Import path resolution
    # ------------------------------------------------------------------

    def _resolve_import_path(self, import_statement: str, from_file: str, root: str) -> str | None:
        """Attempt to resolve an import string to a file path on disk.

        Handles:
        - Relative imports (starting with . or ..)
        - Absolute Python module paths rooted at *root*
        - TypeScript/JS relative paths

        Returns the absolute path if resolvable, else None (external package).
        """
        from_dir = os.path.dirname(from_file)
        ext = os.path.splitext(from_file)[1].lower()

        # TypeScript/JS relative imports
        if import_statement.startswith(".") and ext in (".ts", ".tsx", ".js", ".jsx"):
            candidate = os.path.normpath(os.path.join(from_dir, import_statement))
            for suffix in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                full = candidate + suffix
                if os.path.isfile(full):
                    return full
            return None

        # Python relative imports (e.g. "..utils.helpers")
        if import_statement.startswith(".") and ext == ".py":
            parts = import_statement.split(".")
            up_count = 0
            for p in parts:
                if p == "":
                    up_count += 1
                else:
                    break
            base_dir = from_dir
            for _ in range(up_count - 1):
                base_dir = os.path.dirname(base_dir)
            rest = ".".join(p for p in parts if p)
            candidate = os.path.join(base_dir, rest.replace(".", os.sep))
            for suffix in (".py", "/__init__.py"):
                full = candidate + suffix
                if os.path.isfile(full):
                    return full
            return None

        # Python absolute import from project root
        if ext == ".py" and not import_statement.startswith("."):
            candidate = os.path.join(root, import_statement.replace(".", os.sep))
            for suffix in (".py", "/__init__.py"):
                full = candidate + suffix
                if os.path.isfile(full):
                    return full

        # External / stdlib — not resolvable to a file
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_files(self, root_path: str) -> list[str]:
        """Walk *root_path* recursively and return all supported source files."""
        files: list[str] = []
        skip_dirs = {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            "venv",
            ".mypy_cache",
            ".pytest_cache",
            "dist",
            "build",
            ".next",
        }
        for dirpath, dirnames, filenames in os.walk(root_path):
            # Prune skip directories in-place
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                _, ext = os.path.splitext(fname)
                if ext.lower() in _SUPPORTED_EXTENSIONS:
                    files.append(os.path.join(dirpath, fname))
        return files

    def _guess_root(self, file_path: str) -> str:
        """Walk up from *file_path* looking for a pyproject.toml or package.json."""
        current = os.path.dirname(file_path)
        for _ in range(10):
            for marker in ("pyproject.toml", "package.json", "setup.py", ".git"):
                if os.path.exists(os.path.join(current, marker)):
                    return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return os.path.dirname(file_path)
