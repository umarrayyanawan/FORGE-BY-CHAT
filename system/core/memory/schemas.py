"""Memory engine schemas and data models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

from pydantic import Field

from system.shared.models import TimestampedModel


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    ARCHITECTURAL = "architectural"
    PREFERENCE = "preference"


class MemoryEntry(TimestampedModel):
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    memory_type: MemoryType
    title: str
    content: str
    embedding: list[float] | None = None
    tags: list[str] = []
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: datetime | None = None
    metadata: dict[str, Any] = {}


class ArchitectureDecision(TimestampedModel):
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    title: str
    context: str
    decision: str
    rationale: str
    alternatives_considered: list[str] = []
    consequences: list[str] = []
    status: str = "accepted"
