"""Code Embedder — generates vector embeddings for code chunks and queries.

Uses the OpenAI Embeddings API (text-embedding-3-small by default) and stores
results in a pgvector table ``forge_code_embeddings`` for later similarity
search.
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

import openai
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from system.config.settings import settings
from system.observability.logging.logger import get_logger
from system.repo_intelligence.schemas import CodeChunk, SearchResult
from system.shared.constants import DEFAULT_EMBEDDING_MODEL, MEMORY_SIMILARITY_THRESHOLD
from system.shared.database import AsyncSessionLocal
from system.shared.exceptions import RepoIntelligenceError

logger = get_logger(__name__)

# DDL run once at startup to ensure the table exists
_ENSURE_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS forge_code_embeddings (
    chunk_id    TEXT        NOT NULL,
    file_path   TEXT        NOT NULL,
    project_id  TEXT        NOT NULL,
    chunk_type  TEXT        NOT NULL DEFAULT '',
    name        TEXT        NOT NULL DEFAULT '',
    content     TEXT        NOT NULL,
    language    TEXT        NOT NULL DEFAULT '',
    start_line  INTEGER     NOT NULL DEFAULT 0,
    end_line    INTEGER     NOT NULL DEFAULT 0,
    embedding   vector(1536),
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_fce_project_id
    ON forge_code_embeddings (project_id);

CREATE INDEX IF NOT EXISTS idx_fce_embedding
    ON forge_code_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""


class CodeEmbedder:
    """Generate and store vector embeddings for code chunks."""

    def __init__(self, model: str = DEFAULT_EMBEDDING_MODEL) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        self._initialized = False

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        async with AsyncSessionLocal() as session:
            try:
                for statement in _ENSURE_TABLE_SQL.split(";"):
                    stmt = statement.strip()
                    if stmt:
                        await session.execute(text(stmt))
                await session.commit()
                self._initialized = True
                logger.info("forge_code_embeddings table ready")
            except Exception as exc:
                logger.warning("Could not create embeddings table: %s", exc)
                await session.rollback()
                self._initialized = True  # Don't retry on every call

    # ------------------------------------------------------------------
    # Embedding generation
    # ------------------------------------------------------------------

    async def embed_chunk(self, chunk: CodeChunk) -> List[float]:
        """Embed a single CodeChunk and return the vector."""
        text_input = self._prepare_chunk_text(chunk)
        return await self._embed_text(text_input)

    async def embed_chunks_batch(
        self,
        chunks: List[CodeChunk],
        batch_size: int = 100,
    ) -> List[List[float]]:
        """Embed many chunks in batches, respecting API rate limits.

        Returns a list of float vectors in the same order as *chunks*.
        """
        all_embeddings: List[List[float]] = []
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [self._prepare_chunk_text(c) for c in batch]
            embeddings = await self._embed_texts_batch(texts)
            all_embeddings.extend(embeddings)
            if i + batch_size < len(chunks):
                # Brief pause to avoid hitting rate limits
                await asyncio.sleep(0.5)
        return all_embeddings

    async def embed_query(self, query: str) -> List[float]:
        """Embed a plain-text search query."""
        return await self._embed_text(query)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def upsert_embedding(self, chunk: CodeChunk) -> None:
        """Compute the embedding for *chunk* and upsert it into pgvector."""
        await self._ensure_table()
        if chunk.embedding is None:
            chunk.embedding = await self.embed_chunk(chunk)

        vector_str = "[" + ",".join(str(v) for v in chunk.embedding) + "]"
        upsert_sql = text(
            """
            INSERT INTO forge_code_embeddings
                (chunk_id, file_path, project_id, chunk_type, name,
                 content, language, start_line, end_line, embedding)
            VALUES
                (:chunk_id, :file_path, :project_id, :chunk_type, :name,
                 :content, :language, :start_line, :end_line, :embedding::vector)
            ON CONFLICT (chunk_id, project_id) DO UPDATE SET
                file_path  = EXCLUDED.file_path,
                chunk_type = EXCLUDED.chunk_type,
                name       = EXCLUDED.name,
                content    = EXCLUDED.content,
                language   = EXCLUDED.language,
                start_line = EXCLUDED.start_line,
                end_line   = EXCLUDED.end_line,
                embedding  = EXCLUDED.embedding,
                indexed_at = now()
            """
        )
        async with AsyncSessionLocal() as session:
            await session.execute(
                upsert_sql,
                {
                    "chunk_id": chunk.chunk_id,
                    "file_path": chunk.file_path,
                    "project_id": chunk.project_id,
                    "chunk_type": chunk.chunk_type,
                    "name": chunk.name,
                    "content": chunk.content,
                    "language": chunk.language,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "embedding": vector_str,
                },
            )
            await session.commit()

    async def similarity_search(
        self,
        query_embedding: List[float],
        project_id: str,
        limit: int = 10,
        threshold: float = MEMORY_SIMILARITY_THRESHOLD,
    ) -> List[SearchResult]:
        """Return chunks closest to *query_embedding* within the project.

        Args:
            query_embedding: The query vector (1536 dims).
            project_id: Restrict search to this project.
            limit: Maximum number of results.
            threshold: Minimum cosine similarity to include.

        Returns:
            List of SearchResult sorted by similarity descending.
        """
        await self._ensure_table()
        vector_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

        search_sql = text(
            """
            SELECT
                chunk_id,
                file_path,
                project_id,
                chunk_type,
                name,
                content,
                language,
                start_line,
                end_line,
                1 - (embedding <=> :query_vec::vector) AS similarity
            FROM forge_code_embeddings
            WHERE project_id = :project_id
              AND 1 - (embedding <=> :query_vec::vector) >= :threshold
            ORDER BY embedding <=> :query_vec::vector
            LIMIT :limit
            """
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                search_sql,
                {
                    "query_vec": vector_str,
                    "project_id": project_id,
                    "threshold": threshold,
                    "limit": limit,
                },
            )
            rows = result.fetchall()

        search_results: List[SearchResult] = []
        for row in rows:
            chunk = CodeChunk(
                chunk_id=row.chunk_id,
                file_path=row.file_path,
                project_id=row.project_id,
                chunk_type=row.chunk_type,
                name=row.name,
                content=row.content,
                language=row.language,
                start_line=row.start_line,
                end_line=row.end_line,
            )
            search_results.append(
                SearchResult(
                    chunk=chunk,
                    similarity_score=float(row.similarity),
                    file_path=row.file_path,
                    context=row.content,
                )
            )
        return search_results

    async def delete_project_embeddings(self, project_id: str) -> None:
        """Remove all embeddings for a project."""
        await self._ensure_table()
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("DELETE FROM forge_code_embeddings WHERE project_id = :pid"),
                {"pid": project_id},
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _prepare_chunk_text(self, chunk: CodeChunk) -> str:
        """Build the text that will be embedded for a chunk."""
        parts = [
            f"FILE: {chunk.file_path}",
            f"TYPE: {chunk.chunk_type}",
            f"NAME: {chunk.name}",
        ]
        if chunk.docstring:
            parts.append(f"DOC: {chunk.docstring}")
        parts.append("")
        parts.append(chunk.content)
        return "\n".join(parts)

    async def _embed_text(self, text_input: str) -> List[float]:
        """Call the OpenAI embeddings endpoint for a single string."""
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=text_input[:8191],  # token limit guard
            )
            return response.data[0].embedding
        except openai.RateLimitError:
            logger.warning("OpenAI rate limit hit — waiting 60s")
            await asyncio.sleep(60)
            return await self._embed_text(text_input)
        except Exception as exc:
            raise RepoIntelligenceError(
                f"Embedding generation failed: {exc}",
                details={"model": self._model},
            )

    async def _embed_texts_batch(self, texts: List[str]) -> List[List[float]]:
        """Embed multiple texts in a single API call."""
        truncated = [t[:8191] for t in texts]
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=truncated,
            )
            # API guarantees same order as input
            return [item.embedding for item in sorted(response.data, key=lambda x: x.index)]
        except openai.RateLimitError:
            logger.warning("OpenAI batch rate limit hit — waiting 60s")
            await asyncio.sleep(60)
            return await self._embed_texts_batch(texts)
        except Exception as exc:
            raise RepoIntelligenceError(
                f"Batch embedding failed: {exc}",
                details={"batch_size": len(texts), "model": self._model},
            )
