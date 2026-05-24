"""Repo Indexer — orchestrates AST parsing, embedding, and graph construction.

Entry point for indexing a whole project or performing incremental updates
when individual files change.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import List

from system.observability.logging.logger import get_logger
from system.repo_intelligence.ast.parser import ASTParser
from system.repo_intelligence.dependency_mapping.mapper import DependencyMapper
from system.repo_intelligence.embeddings.embedder import CodeEmbedder
from system.repo_intelligence.graph.builder import GraphBuilder
from system.repo_intelligence.schemas import CodeChunk, IndexingResult
from system.shared.exceptions import RepoIntelligenceError

logger = get_logger(__name__)

_SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
}


class RepoIndexer:
    """Orchestrate full and incremental project indexing."""

    def __init__(
        self,
        ast_parser: ASTParser,
        embedder: CodeEmbedder,
        graph_builder: GraphBuilder,
        mapper: DependencyMapper,
    ) -> None:
        self._parser = ast_parser
        self._embedder = embedder
        self._graph = graph_builder
        self._mapper = mapper

    # ------------------------------------------------------------------
    # Full project indexing
    # ------------------------------------------------------------------

    async def index_project(
        self,
        project_id: str,
        root_path: str,
    ) -> IndexingResult:
        """Index an entire project from scratch.

        Steps:
        1. Walk all .py / .ts / .tsx files.
        2. Parse each with the AST parser.
        3. Embed all chunks in batches of 100.
        4. Build / update the Neo4j dependency graph.
        5. Map file-level dependencies.
        6. Return IndexingResult with statistics.

        Args:
            project_id: FORGE project identifier.
            root_path: Absolute path to the repository root.

        Returns:
            IndexingResult with files_indexed, chunks_created, time_seconds, errors.
        """
        if not os.path.isdir(root_path):
            raise RepoIntelligenceError(
                f"Root path does not exist: {root_path}",
                details={"project_id": project_id, "root_path": root_path},
            )

        t_start = time.monotonic()
        errors: List[str] = []
        all_files = self._collect_files(root_path)
        all_chunks: List[CodeChunk] = []

        logger.info(
            "RepoIndexer: indexing project=%s root=%s files=%d",
            project_id,
            root_path,
            len(all_files),
        )

        # ---- Phase 1: Parse ----
        for file_path in all_files:
            try:
                chunks = self._parser.parse_file(file_path, project_id)
                all_chunks.extend(chunks)
            except RepoIntelligenceError as exc:
                err_msg = f"Parse error [{file_path}]: {exc.message}"
                errors.append(err_msg)
                logger.warning(err_msg)

        logger.info(
            "Parsed %d files → %d chunks (project=%s)",
            len(all_files),
            len(all_chunks),
            project_id,
        )

        # ---- Phase 2: Embed ----
        try:
            embeddings = await self._embedder.embed_chunks_batch(
                all_chunks, batch_size=100
            )
            for chunk, emb in zip(all_chunks, embeddings):
                chunk.embedding = emb
                await self._embedder.upsert_embedding(chunk)
        except Exception as exc:
            err_msg = f"Embedding phase failed: {exc}"
            errors.append(err_msg)
            logger.error(err_msg)

        # ---- Phase 3: Graph build ----
        try:
            await self._graph.build_project_graph(project_id, all_chunks)
        except Exception as exc:
            err_msg = f"Graph build failed: {exc}"
            errors.append(err_msg)
            logger.error(err_msg)

        # ---- Phase 4: Dependency mapping ----
        try:
            await self._mapper.map_project(project_id, root_path)
        except Exception as exc:
            err_msg = f"Dependency mapping failed: {exc}"
            errors.append(err_msg)
            logger.error(err_msg)

        elapsed = time.monotonic() - t_start
        result = IndexingResult(
            project_id=project_id,
            files_indexed=len(all_files),
            chunks_created=len(all_chunks),
            time_seconds=round(elapsed, 3),
            errors=errors,
        )
        logger.info(
            "Indexing complete: project=%s files=%d chunks=%d time=%.2fs errors=%d",
            project_id,
            result.files_indexed,
            result.chunks_created,
            result.time_seconds,
            len(errors),
        )
        return result

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    async def update_index(
        self, project_id: str, changed_files: List[str]
    ) -> None:
        """Incrementally re-index only the changed files.

        Removes stale embeddings and chunk nodes for each file, then
        re-parses, re-embeds, and rebuilds graph nodes.

        Args:
            project_id: FORGE project identifier.
            changed_files: List of absolute file paths that changed.
        """
        logger.info(
            "Incremental update: project=%s changed=%d",
            project_id,
            len(changed_files),
        )

        for file_path in changed_files:
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue

            try:
                # Re-parse
                chunks = self._parser.parse_file(file_path, project_id)
            except RepoIntelligenceError as exc:
                logger.warning("Skipping %s in update: %s", file_path, exc.message)
                continue

            # Re-embed and upsert
            if chunks:
                try:
                    embeddings = await self._embedder.embed_chunks_batch(chunks)
                    for chunk, emb in zip(chunks, embeddings):
                        chunk.embedding = emb
                        await self._embedder.upsert_embedding(chunk)
                except Exception as exc:
                    logger.error("Embedding failed for %s: %s", file_path, exc)

                # Update graph nodes
                try:
                    await self._mapper.update_file(project_id, file_path)
                except Exception as exc:
                    logger.error("Graph update failed for %s: %s", file_path, exc)

        logger.info("Incremental update complete for %d files", len(changed_files))

    # ------------------------------------------------------------------
    # Delete index
    # ------------------------------------------------------------------

    async def delete_index(self, project_id: str) -> None:
        """Remove all indexed data for *project_id*.

        Deletes:
        - All pgvector embeddings for the project.
        - All Neo4j nodes and relationships for the project.
        """
        logger.info("Deleting index for project=%s", project_id)
        await asyncio.gather(
            self._embedder.delete_project_embeddings(project_id),
            self._graph.delete_project_graph(project_id),
        )
        logger.info("Index deleted for project=%s", project_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_files(self, root_path: str) -> List[str]:
        """Recursively collect all supported source files under *root_path*."""
        files: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                _, ext = os.path.splitext(fname)
                if ext.lower() in _SUPPORTED_EXTENSIONS:
                    files.append(os.path.join(dirpath, fname))
        return files
