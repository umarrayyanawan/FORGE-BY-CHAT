"""Dependency Graph Builder — stores repo structure in Neo4j.

Creates File nodes, CodeChunk nodes, and the relationships between them:
  - (File)-[:CONTAINS]->(CodeChunk)
  - (File)-[:IMPORTS]->(File)
  - (CodeChunk)-[:EXTENDS]->(CodeChunk)
  - (CodeChunk)-[:CALLS]->(CodeChunk)
"""

from __future__ import annotations

import json
from typing import Any

from system.observability.logging.logger import get_logger
from system.repo_intelligence.schemas import (
    CodeChunk,
    DependencyEdge,
    FileNode,
)
from system.shared.neo4j_client import NeoDB

logger = get_logger(__name__)


class GraphBuilder:
    """Build and query the dependency graph in Neo4j."""

    def __init__(self, neo4j: NeoDB) -> None:
        self._db = neo4j

    # ------------------------------------------------------------------
    # Graph bootstrap
    # ------------------------------------------------------------------

    async def ensure_constraints(self) -> None:
        """Create unique constraints if they don't exist."""
        constraints = [
            "CREATE CONSTRAINT forge_file_unique IF NOT EXISTS "
            "FOR (f:ForgeFile) REQUIRE (f.project_id, f.path) IS UNIQUE",
            "CREATE CONSTRAINT forge_chunk_unique IF NOT EXISTS "
            "FOR (c:ForgeChunk) REQUIRE c.chunk_id IS UNIQUE",
        ]
        for cypher in constraints:
            try:
                await self._db.run_query(cypher)
            except Exception as exc:
                logger.debug("Constraint creation skipped: %s", exc)

    # ------------------------------------------------------------------
    # Project graph construction
    # ------------------------------------------------------------------

    async def build_project_graph(
        self,
        project_id: str,
        chunks: list[CodeChunk],
    ) -> None:
        """Build the full project dependency graph from parsed chunks.

        Steps:
        1. Create/merge File nodes for each unique file.
        2. Create/merge CodeChunk nodes.
        3. Create CONTAINS edges from File -> CodeChunk.
        4. Create IMPORTS edges between files.
        5. Create EXTENDS/IMPLEMENTS edges for class hierarchies.
        """
        await self.ensure_constraints()

        # Group chunks by file
        files_seen: dict[str, list[CodeChunk]] = {}
        for chunk in chunks:
            files_seen.setdefault(chunk.file_path, []).append(chunk)

        # Create file nodes
        for file_path, file_chunks in files_seen.items():
            file_node = FileNode(
                path=file_path,
                language=file_chunks[0].language if file_chunks else "unknown",
                size_bytes=0,
                chunk_count=len(file_chunks),
                import_count=len(file_chunks[0].dependencies) if file_chunks else 0,
                export_count=len(file_chunks[0].exports) if file_chunks else 0,
                last_modified=__import__("datetime").datetime.utcnow(),
                project_id=project_id,
            )
            await self.upsert_file_node(project_id, file_node)

        # Create chunk nodes and CONTAINS edges
        for chunk in chunks:
            await self.upsert_chunk_node(project_id, chunk)
            await self._create_contains_edge(project_id, chunk.file_path, chunk.chunk_id)

        # Create dependency edges
        for chunk in chunks:
            for dep in chunk.dependencies:
                # Try to resolve the dep to a file path within the project
                # Store the raw dep string as the to_file target
                edge = DependencyEdge(
                    from_file=chunk.file_path,
                    to_file=dep,
                    dependency_type="import",
                    symbols=[],
                )
                await self.create_dependency_edge(project_id, edge)

    # ------------------------------------------------------------------
    # Node upsert
    # ------------------------------------------------------------------

    async def upsert_file_node(self, project_id: str, file_node: FileNode) -> None:
        """Create or update a File node in Neo4j."""
        cypher = """
        MERGE (f:ForgeFile {project_id: $project_id, path: $path})
        SET f.language     = $language,
            f.size_bytes   = $size_bytes,
            f.chunk_count  = $chunk_count,
            f.import_count = $import_count,
            f.export_count = $export_count,
            f.project_id   = $project_id,
            f.updated_at   = datetime()
        """
        await self._db.run_query(
            cypher,
            {
                "project_id": project_id,
                "path": file_node.path,
                "language": file_node.language,
                "size_bytes": file_node.size_bytes,
                "chunk_count": file_node.chunk_count,
                "import_count": file_node.import_count,
                "export_count": file_node.export_count,
            },
        )

    async def upsert_chunk_node(self, project_id: str, chunk: CodeChunk) -> None:
        """Create or update a CodeChunk node in Neo4j."""
        cypher = """
        MERGE (c:ForgeChunk {chunk_id: $chunk_id})
        SET c.file_path   = $file_path,
            c.chunk_type  = $chunk_type,
            c.name        = $name,
            c.start_line  = $start_line,
            c.end_line    = $end_line,
            c.language    = $language,
            c.project_id  = $project_id,
            c.docstring   = $docstring,
            c.exports     = $exports,
            c.updated_at  = datetime()
        """
        await self._db.run_query(
            cypher,
            {
                "chunk_id": chunk.chunk_id,
                "file_path": chunk.file_path,
                "chunk_type": chunk.chunk_type,
                "name": chunk.name,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "language": chunk.language,
                "project_id": project_id,
                "docstring": chunk.docstring or "",
                "exports": json.dumps(chunk.exports),
            },
        )

    # ------------------------------------------------------------------
    # Edge creation
    # ------------------------------------------------------------------

    async def _create_contains_edge(self, project_id: str, file_path: str, chunk_id: str) -> None:
        cypher = """
        MATCH (f:ForgeFile {project_id: $project_id, path: $file_path})
        MATCH (c:ForgeChunk {chunk_id: $chunk_id, project_id: $project_id})
        MERGE (f)-[:CONTAINS]->(c)
        """
        await self._db.run_query(
            cypher,
            {
                "project_id": project_id,
                "file_path": file_path,
                "chunk_id": chunk_id,
            },
        )

    async def create_dependency_edge(self, project_id: str, edge: DependencyEdge) -> None:
        """Create an IMPORTS / EXTENDS / IMPLEMENTS / USES edge between files."""
        rel_type_map = {
            "import": "IMPORTS",
            "extends": "EXTENDS",
            "implements": "IMPLEMENTS",
            "uses": "USES",
        }
        rel = rel_type_map.get(edge.dependency_type, "DEPENDS_ON")

        # Merge target file node even if it's external (no chunk_count)
        await self._db.run_query(
            "MERGE (f:ForgeFile {project_id: $pid, path: $path}) ON CREATE SET f.external = true",
            {"pid": project_id, "path": edge.to_file},
        )

        cypher = f"""
        MATCH (a:ForgeFile {{project_id: $project_id, path: $from_file}})
        MATCH (b:ForgeFile {{project_id: $project_id, path: $to_file}})
        MERGE (a)-[r:{rel}]->(b)
        SET r.symbols    = $symbols,
            r.updated_at = datetime()
        """
        await self._db.run_query(
            cypher,
            {
                "project_id": project_id,
                "from_file": edge.from_file,
                "to_file": edge.to_file,
                "symbols": json.dumps(edge.symbols),
            },
        )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def get_file_dependencies(self, project_id: str, file_path: str) -> list[str]:
        """Return direct dependency file paths for *file_path*."""
        cypher = """
        MATCH (f:ForgeFile {project_id: $pid, path: $path})-[:IMPORTS]->(dep:ForgeFile)
        RETURN dep.path AS dep_path
        """
        records = await self._db.run_query(cypher, {"pid": project_id, "path": file_path})
        return [r["dep_path"] for r in records if r.get("dep_path")]

    async def get_transitive_dependencies(
        self, project_id: str, file_path: str, depth: int = 3
    ) -> list[str]:
        """Return all transitive dependency paths up to *depth* hops."""
        depth = min(max(1, depth), 10)
        cypher = f"""
        MATCH (f:ForgeFile {{project_id: $pid, path: $path}})
              -[:IMPORTS*1..{depth}]->(dep:ForgeFile)
        RETURN DISTINCT dep.path AS dep_path
        """
        records = await self._db.run_query(cypher, {"pid": project_id, "path": file_path})
        return [r["dep_path"] for r in records if r.get("dep_path")]

    async def find_cycles(self, project_id: str) -> list[list[str]]:
        """Detect circular dependency chains in the project graph."""
        cypher = """
        MATCH path = (f:ForgeFile {project_id: $pid})-[:IMPORTS*2..10]->(f)
        RETURN [node IN nodes(path) | node.path] AS cycle
        LIMIT 50
        """
        records = await self._db.run_query(cypher, {"pid": project_id})
        cycles: list[list[str]] = []
        seen: set = set()
        for r in records:
            cycle = r.get("cycle", [])
            if cycle:
                key = tuple(sorted(set(cycle)))
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
        return cycles

    async def get_impact_set(self, project_id: str, file_path: str) -> list[str]:
        """Return all files that import (directly or transitively) *file_path*."""
        cypher = """
        MATCH (dep:ForgeFile {project_id: $pid, path: $path})
              <-[:IMPORTS*1..10]-(importer:ForgeFile)
        RETURN DISTINCT importer.path AS imp_path
        """
        records = await self._db.run_query(cypher, {"pid": project_id, "path": file_path})
        return [r["imp_path"] for r in records if r.get("imp_path")]

    async def get_architecture_overview(self, project_id: str) -> dict[str, Any]:
        """Return a high-level summary of the project graph."""
        stats_cypher = """
        MATCH (f:ForgeFile {project_id: $pid})
        OPTIONAL MATCH (f)-[:CONTAINS]->(c:ForgeChunk)
        OPTIONAL MATCH (f)-[:IMPORTS]->(dep:ForgeFile)
        RETURN
            count(DISTINCT f)   AS file_count,
            count(DISTINCT c)   AS chunk_count,
            count(DISTINCT dep) AS dep_count
        """
        stats = await self._db.run_query(stats_cypher, {"pid": project_id})

        # Top-level files (not imported by anyone — likely entry points)
        entry_cypher = """
        MATCH (f:ForgeFile {project_id: $pid})
        WHERE NOT ()-[:IMPORTS]->(f)
          AND NOT coalesce(f.external, false)
        RETURN f.path AS path
        LIMIT 20
        """
        entry_records = await self._db.run_query(entry_cypher, {"pid": project_id})

        # Most-imported files
        hub_cypher = """
        MATCH (f:ForgeFile {project_id: $pid})<-[:IMPORTS]-(importer)
        RETURN f.path AS path, count(importer) AS import_count
        ORDER BY import_count DESC
        LIMIT 10
        """
        hub_records = await self._db.run_query(hub_cypher, {"pid": project_id})

        s = stats[0] if stats else {}
        return {
            "project_id": project_id,
            "file_count": s.get("file_count", 0),
            "chunk_count": s.get("chunk_count", 0),
            "dependency_count": s.get("dep_count", 0),
            "entry_points": [r["path"] for r in entry_records],
            "most_imported": [
                {"path": r["path"], "import_count": r["import_count"]} for r in hub_records
            ],
        }

    async def delete_project_graph(self, project_id: str) -> None:
        """Remove all nodes and edges for a project."""
        await self._db.run_query(
            "MATCH (n {project_id: $pid}) DETACH DELETE n",
            {"pid": project_id},
        )

    async def get_chunk_by_file(self, project_id: str, file_path: str) -> list[dict[str, Any]]:
        """Return chunk metadata for all chunks in a file."""
        cypher = """
        MATCH (f:ForgeFile {project_id: $pid, path: $path})-[:CONTAINS]->(c:ForgeChunk)
        RETURN c.chunk_id AS chunk_id, c.name AS name, c.chunk_type AS chunk_type,
               c.start_line AS start_line, c.end_line AS end_line
        ORDER BY c.start_line
        """
        return await self._db.run_query(cypher, {"pid": project_id, "path": file_path})
