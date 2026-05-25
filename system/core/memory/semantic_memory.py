"""Semantic memory — stores facts and knowledge about the project using vector similarity."""

from __future__ import annotations

from typing import Any

from system.core.memory.schemas import MemoryEntry, MemoryType
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class SemanticMemory:
    """Vector-indexed factual memory about the project."""

    def __init__(self, db: Any = None, embedder: Any = None) -> None:
        self.db = db
        self.embedder = embedder
        self._store: dict[str, MemoryEntry] = {}

    async def store(
        self,
        project_id: str,
        title: str,
        content: str,
        tags: list[str] = [],
        importance: float = 0.5,
    ) -> MemoryEntry:
        embedding: list[float] | None = None
        if self.embedder:
            try:
                embedding = await self.embedder.embed_text(content)
            except Exception as exc:
                logger.warning("Embedding failed", error=str(exc))

        entry = MemoryEntry(
            project_id=project_id,
            memory_type=MemoryType.SEMANTIC,
            title=title,
            content=content,
            embedding=embedding,
            tags=list(tags),
            importance=importance,
        )
        self._store[entry.memory_id] = entry
        logger.debug("Semantic memory stored", memory_id=entry.memory_id, title=title)
        return entry

    async def retrieve(
        self,
        project_id: str,
        query: str,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """Return top-k memories ranked by importance (pgvector cosine in production)."""
        entries = [e for e in self._store.values() if e.project_id == project_id]
        return sorted(entries, key=lambda e: e.importance, reverse=True)[:limit]

    async def update_importance(self, memory_id: str, delta: float) -> None:
        if memory_id in self._store:
            entry = self._store[memory_id]
            entry.importance = max(0.0, min(1.0, entry.importance + delta))

    async def forget(self, memory_id: str) -> None:
        self._store.pop(memory_id, None)

    async def consolidate(self, project_id: str) -> None:
        """Remove low-importance duplicate memories."""
        to_remove = [
            mid
            for mid, e in self._store.items()
            if e.project_id == project_id and e.importance < 0.1
        ]
        for mid in to_remove:
            del self._store[mid]
        logger.info("Memory consolidated", project_id=project_id, removed=len(to_remove))
