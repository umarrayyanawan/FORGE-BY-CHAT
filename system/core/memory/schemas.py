"""Memory engine schemas and data models."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from system.shared.models import BaseForgeModel, TimestampedModel


class MemoryType(str, Enum):
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
    embedding: Optional[List[float]] = None
    tags: List[str] = []
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    access_count: int = 0
    last_accessed: Optional[datetime] = None
    metadata: Dict[str, Any] = {}


class ArchitectureDecision(TimestampedModel):
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    title: str
    context: str
    decision: str
    rationale: str
    alternatives_considered: List[str] = []
    consequences: List[str] = []
    status: str = "accepted"
