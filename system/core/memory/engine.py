"""MemoryEngine — central interface for all memory operations in FORGE."""

from __future__ import annotations

from typing import Any

from system.core.memory.episodic_memory import EpisodicMemory
from system.core.memory.retrieval import MemoryRetriever
from system.core.memory.schemas import ArchitectureDecision
from system.core.memory.semantic_memory import SemanticMemory
from system.core.memory.summarizer import MemorySummarizer
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class MemoryEngine:
    def __init__(
        self,
        semantic: SemanticMemory,
        episodic: EpisodicMemory,
        retriever: MemoryRetriever,
        summarizer: MemorySummarizer,
        db: Any = None,
    ) -> None:
        self.semantic = semantic
        self.episodic = episodic
        self.retriever = retriever
        self.summarizer = summarizer
        self.db = db
        self._adrs: list[ArchitectureDecision] = []

    async def store_architecture_decision(
        self, project_id: str, decision: ArchitectureDecision
    ) -> None:
        self._adrs.append(decision)
        await self.semantic.store(
            project_id,
            decision.title,
            f"{decision.context}\n{decision.decision}\n{decision.rationale}",
            tags=["adr", "architecture"],
            importance=0.9,
        )

    async def get_context_for_agent(self, project_id: str, agent_type: Any, task: Any) -> str:
        """Return enriched context string for agent prompt injection."""
        memories = await self.retriever.retrieve_for_agent(project_id, agent_type, task.description)
        if not memories:
            return ""
        summary = await self.summarizer.summarize_memories(memories)
        return f"## Relevant Project Memory\n{summary}"

    async def learn_from_failure(self, project_id: str, task: Any, error: str, fix: str) -> None:
        await self.episodic.record_fix(task.task_id, error, fix, project_id)
        await self.semantic.store(
            project_id,
            f"Fix: {error[:50]}",
            f"When this error occurs: {error}\nApply this fix: {fix}",
            tags=["fix", "error"],
            importance=0.85,
        )

    async def project_summary(self, project_id: str) -> str:
        history = await self.episodic.summarize_project_history(project_id)
        semantic_memories = await self.semantic.retrieve(project_id, "project overview", limit=5)
        semantic_summary = await self.summarizer.summarize_memories(semantic_memories)
        return f"# Project Summary\n\n## Events\n{history}\n\n## Key Knowledge\n{semantic_summary}"
