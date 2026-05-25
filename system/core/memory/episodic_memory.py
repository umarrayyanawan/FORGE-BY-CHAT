"""Episodic memory — stores specific events: task outcomes, deployments, fixes."""

from __future__ import annotations

from typing import Any

from system.core.memory.schemas import MemoryEntry, MemoryType
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class EpisodicMemory:
    """Chronological event log with semantic retrieval."""

    def __init__(self, db: Any = None, redis: Any = None, llm_client: Any = None) -> None:
        self.db = db
        self.redis = redis
        self.llm_client = llm_client
        self._episodes: list[MemoryEntry] = []

    async def record_task(
        self, task_id: str, project_id: str, outcome: str, details: dict[str, Any]
    ) -> MemoryEntry:
        entry = MemoryEntry(
            project_id=project_id,
            memory_type=MemoryType.EPISODIC,
            title=f"Task {task_id}: {outcome}",
            content=f"Task {task_id} {outcome}. Details: {details}",
            importance=0.7 if outcome == "failed" else 0.4,
            metadata={"task_id": task_id, "outcome": outcome, **details},
        )
        self._episodes.append(entry)
        return entry

    async def record_deployment(
        self, deployment_id: str, project_id: str, outcome: str, details: dict[str, Any]
    ) -> MemoryEntry:
        entry = MemoryEntry(
            project_id=project_id,
            memory_type=MemoryType.EPISODIC,
            title=f"Deployment {deployment_id}: {outcome}",
            content=f"Deployment to {details.get('target', 'unknown')}: {outcome}",
            importance=0.8,
            metadata={"deployment_id": deployment_id, "outcome": outcome, **details},
        )
        self._episodes.append(entry)
        return entry

    async def record_fix(self, task_id: str, error: str, fix: str, project_id: str) -> MemoryEntry:
        entry = MemoryEntry(
            project_id=project_id,
            memory_type=MemoryType.EPISODIC,
            title=f"Fix applied for task {task_id}",
            content=f"Error: {error}\nFix: {fix}",
            importance=0.9,
            metadata={"task_id": task_id, "error": error, "fix": fix},
        )
        self._episodes.append(entry)
        return entry

    async def get_project_timeline(self, project_id: str) -> list[MemoryEntry]:
        return [e for e in self._episodes if e.project_id == project_id]

    async def get_similar_episodes(
        self, project_id: str, description: str, limit: int = 5
    ) -> list[MemoryEntry]:
        entries = [e for e in self._episodes if e.project_id == project_id]
        return sorted(entries, key=lambda e: e.importance, reverse=True)[:limit]

    async def summarize_project_history(self, project_id: str) -> str:
        episodes = await self.get_project_timeline(project_id)
        if not episodes:
            return "No history recorded yet."
        lines = [f"- {e.title}" for e in episodes[-20:]]
        return "Project history:\n" + "\n".join(lines)
