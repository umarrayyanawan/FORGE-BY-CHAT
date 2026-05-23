"""System-wide constants for the FORGE platform.

Import from here instead of scattering magic strings / numbers across modules.
"""

from __future__ import annotations

# ========================================================================== #
# Versioning
# ========================================================================== #

FORGE_VERSION: str = "0.1.0"

# ========================================================================== #
# Intent & clarification
# ========================================================================== #

MAX_INTENT_CLARIFICATION_ROUNDS: int = 5
"""Maximum back-and-forth rounds to collect user clarifications before giving up."""

# ========================================================================== #
# Agent runtime limits
# ========================================================================== #

MAX_AGENT_RETRIES: int = 3
"""Number of times an agent will retry a failed operation before raising."""

MAX_TASK_EXECUTION_TIME_SECONDS: int = 3_600
"""Hard wall-clock deadline for a single task — 1 hour."""

MAX_TOKENS_PER_AGENT: int = 8_192
"""Default maximum completion tokens allocated per agent call."""

SCOPED_CONTEXT_MAX_FILES: int = 20
"""Maximum number of repo files injected into a single agent context window."""

# ========================================================================== #
# AI / LLM defaults
# ========================================================================== #

DEFAULT_LLM_MODEL: str = "claude-3-5-sonnet-20241022"
"""Default Anthropic model used when no override is provided."""

DEFAULT_LLM_TEMPERATURE: float = 0.1
"""Low temperature for deterministic, production-grade code generation."""

DEFAULT_EMBEDDING_MODEL: str = "text-embedding-3-small"
"""OpenAI embedding model used for semantic search and memory retrieval."""

# ========================================================================== #
# Task / queue identifiers
# ========================================================================== #

TASK_QUEUE_DEFAULT: str = "forge.tasks"
"""Celery / Temporal queue for standard-priority work."""

TASK_QUEUE_PRIORITY: str = "forge.priority"
"""Celery / Temporal queue for high-priority / interactive work."""

# ========================================================================== #
# Redis channel names
# ========================================================================== #

EVENT_BUS_CHANNEL: str = "forge:events"
"""Redis Pub/Sub channel used by the internal event bus."""

# ========================================================================== #
# Redis key prefixes
# ========================================================================== #

KEY_PREFIX_INTENT: str = "FORGE:INTENT:"
KEY_PREFIX_SESSION: str = "FORGE:SESSION:"
KEY_PREFIX_TASK: str = "FORGE:TASK:"
KEY_PREFIX_MEMORY: str = "FORGE:MEMORY:"

# ========================================================================== #
# Memory / retrieval
# ========================================================================== #

MEMORY_SIMILARITY_THRESHOLD: float = 0.75
"""Minimum cosine similarity score for a memory hit to be considered relevant."""

# ========================================================================== #
# API pagination
# ========================================================================== #

DEFAULT_PAGE_SIZE: int = 20
MAX_PAGE_SIZE: int = 100

# ========================================================================== #
# HTTP timeouts (seconds)
# ========================================================================== #

HTTP_CONNECT_TIMEOUT: float = 5.0
HTTP_READ_TIMEOUT: float = 60.0
