"""Semantic Search Retriever — combines vector search with graph centrality.

Retrieval pipeline:
  1. Embed the query with OpenAI.
  2. Vector similarity search in pgvector (forge_code_embeddings).
  3. Re-rank results by blending similarity score + graph in-degree centrality.
  4. Optionally expand context with surrounding file lines.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from system.observability.logging.logger import get_logger
from system.repo_intelligence.embeddings.embedder import CodeEmbedder
from system.repo_intelligence.graph.builder import GraphBuilder
from system.repo_intelligence.schemas import CodeChunk, SearchResult
from system.shared.constants import MEMORY_SIMILARITY_THRESHOLD
from system.shared.exceptions import RepoIntelligenceError

logger = get_logger(__name__)

# Weight applied to graph centrality during re-ranking (0–1).
_CENTRALITY_WEIGHT = 0.15
# Weight applied to similarity score during re-ranking.
_SIMILARITY_WEIGHT = 0.85


class SemanticRetriever:
    """Semantic search over an indexed codebase."""

    def __init__(
        self,
        embedder: CodeEmbedder,
        graph_builder: GraphBuilder,
    ) -> None:
        self._embedder = embedder
        self._graph = graph_builder

    # ------------------------------------------------------------------
    # Primary search
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        project_id: str,
        limit: int = 10,
    ) -> List[SearchResult]:
        """Full semantic search pipeline.

        1. Embed the query.
        2. pgvector similarity search.
        3. Re-rank with graph centrality.
        4. Return top *limit* results with context.

        Args:
            query: Natural language or code snippet to search for.
            project_id: Restrict to this project.
            limit: Maximum results to return.

        Returns:
            Sorted list of SearchResult objects (best match first).
        """
        query_vec = await self._embedder.embed_query(query)
        raw_results = await self._embedder.similarity_search(
            query_embedding=query_vec,
            project_id=project_id,
            limit=limit * 3,  # over-fetch for re-ranking
            threshold=MEMORY_SIMILARITY_THRESHOLD - 0.1,
        )
        if not raw_results:
            return []

        # Build centrality map for the files returned
        file_paths = list({r.file_path for r in raw_results})
        centrality_map = await self._compute_centrality(project_id, file_paths)

        # Re-rank
        scored: List[tuple[float, SearchResult]] = []
        for result in raw_results:
            centrality = centrality_map.get(result.file_path, 0.0)
            combined = (
                _SIMILARITY_WEIGHT * result.similarity_score
                + _CENTRALITY_WEIGHT * centrality
            )
            scored.append((combined, result))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:limit]

        # Add surrounding file context
        enriched: List[SearchResult] = []
        for _, result in top:
            context = self._add_surrounding_context(
                result.chunk,
                self._read_file_safe(result.file_path),
            )
            enriched.append(
                SearchResult(
                    chunk=result.chunk,
                    similarity_score=result.similarity_score,
                    file_path=result.file_path,
                    context=context,
                )
            )
        return enriched

    # ------------------------------------------------------------------
    # Task context injection
    # ------------------------------------------------------------------

    async def get_relevant_context_for_task(
        self,
        task_description: str,
        project_id: str,
        max_files: int = 20,
    ) -> Dict[str, str]:
        """Return {file_path: relevant_content} for agent context injection.

        Retrieves the most semantically relevant code sections for a task,
        grouped by file.

        Args:
            task_description: The task or user story to find context for.
            project_id: Project to search within.
            max_files: Cap on distinct files included.

        Returns:
            Dict mapping file path -> concatenated relevant excerpts.
        """
        results = await self.search(
            query=task_description,
            project_id=project_id,
            limit=max_files * 3,
        )

        context_map: Dict[str, List[str]] = {}
        for result in results:
            fp = result.file_path
            if len(context_map) >= max_files and fp not in context_map:
                continue
            context_map.setdefault(fp, []).append(result.context)

        return {fp: "\n\n---\n\n".join(excerpts) for fp, excerpts in context_map.items()}

    # ------------------------------------------------------------------
    # Similar code search
    # ------------------------------------------------------------------

    async def find_similar_code(
        self, code_snippet: str, project_id: str
    ) -> List[SearchResult]:
        """Find code chunks semantically similar to *code_snippet*."""
        return await self.search(
            query=code_snippet,
            project_id=project_id,
            limit=10,
        )

    # ------------------------------------------------------------------
    # File summary
    # ------------------------------------------------------------------

    async def get_file_summary(self, file_path: str, project_id: str) -> str:
        """Return a brief semantic summary of a file.

        Retrieves the module-level docstring and class/function names
        from indexed chunks.

        Args:
            file_path: Path to the file.
            project_id: Project ID.

        Returns:
            A human-readable summary string.
        """
        chunks = await self._graph.get_chunk_by_file(project_id, file_path)
        if not chunks:
            return f"No indexed data found for {file_path}"

        lines = [f"File: {file_path}", f"Indexed chunks: {len(chunks)}", ""]
        module_doc: Optional[str] = None
        definitions: List[str] = []

        for chunk in chunks:
            ctype = chunk.get("chunk_type", "")
            name = chunk.get("name", "")
            if ctype == "module" and not module_doc:
                module_doc = name
            elif ctype in ("class", "function", "method"):
                definitions.append(f"  [{ctype}] {name}")

        if module_doc:
            lines.append(f"Module: {module_doc}")
        if definitions:
            lines.append("Definitions:")
            lines.extend(definitions[:20])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _add_surrounding_context(
        self,
        chunk: CodeChunk,
        file_content: str,
        context_lines: int = 5,
    ) -> str:
        """Expand a chunk's content with *context_lines* above and below."""
        if not file_content:
            return chunk.content

        all_lines = file_content.splitlines()
        total = len(all_lines)
        start = max(0, chunk.start_line - 1 - context_lines)
        end = min(total, chunk.end_line + context_lines)
        context_slice = all_lines[start:end]
        return "\n".join(context_slice)

    def _read_file_safe(self, file_path: str) -> str:
        """Read file content without raising on missing files."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Graph centrality scoring
    # ------------------------------------------------------------------

    async def _compute_centrality(
        self, project_id: str, file_paths: List[str]
    ) -> Dict[str, float]:
        """Compute a normalised in-degree centrality for each file.

        Files imported by many others are considered more central and
        receive a higher score (0.0–1.0).
        """
        if not file_paths:
            return {}

        # Use impact_set size as a proxy for centrality
        centrality: Dict[str, int] = {}
        for fp in file_paths:
            try:
                impact = await self._graph.get_impact_set(project_id, fp)
                centrality[fp] = len(impact)
            except Exception:
                centrality[fp] = 0

        max_val = max(centrality.values(), default=1) or 1
        return {fp: count / max_val for fp, count in centrality.items()}
