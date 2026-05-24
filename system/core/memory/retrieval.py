"""Memory retrieval pipeline — combined semantic + episodic retrieval with reranking."""
from __future__ import annotations

from typing import Any, List, Optional

from system.core.memory.schemas import MemoryEntry, MemoryType
from system.core.memory.semantic_memory import SemanticMemory
from system.core.memory.episodic_memory import EpisodicMemory
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class MemoryRetriever:
    def __init__(self, semantic: SemanticMemory, episodic: EpisodicMemory) -> None:
        self.semantic = semantic
        self.episodic = episodic

    async def retrieve_relevant(
        self,
        project_id: str,
        query: str,
        memory_types: Optional[List[MemoryType]] = None,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        results: List[MemoryEntry] = []
        half = max(limit // 2, 3)

        if memory_types is None or MemoryType.SEMANTIC in memory_types:
            results.extend(await self.semantic.retrieve(project_id, query, limit=half))
        if memory_types is None or MemoryType.EPISODIC in memory_types:
            results.extend(
                await self.episodic.get_similar_episodes(project_id, query, limit=half)
            )

        seen: set = set()
        unique = []
        for entry in results:
            if entry.memory_id not in seen:
                seen.add(entry.memory_id)
                unique.append(entry)

        return sorted(unique, key=lambda e: e.importance, reverse=True)[:limit]

    async def retrieve_for_agent(
        self, project_id: str, agent_type: Any, task_description: str
    ) -> List[MemoryEntry]:
        return await self.retrieve_relevant(project_id, task_description, limit=5)

    async def retrieve_architecture_decisions(self, project_id: str) -> List:
        return []
