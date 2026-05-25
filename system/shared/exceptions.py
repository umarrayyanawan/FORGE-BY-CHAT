"""FORGE custom exception hierarchy.

All exceptions carry a human-readable ``message``, a machine-readable
``code`` (SCREAMING_SNAKE_CASE), and an optional ``details`` dict for
structured context that can be serialised into API error responses.
"""

from __future__ import annotations

from typing import Any


class ForgeError(Exception):
    """Base class for all FORGE application exceptions."""

    # Subclasses should override this at class level.
    code: str = "FORGE_ERROR"

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        # Allow per-instance override; fall back to class-level default.
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}
        super().__init__(message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON API error payloads."""
        return {
            "error": self.message,
            "code": self.code,
            "details": self.details,
        }


# --------------------------------------------------------------------------- #
# Domain-specific exceptions
# --------------------------------------------------------------------------- #


class ValidationError(ForgeError):
    """Raised when input data fails schema or business-rule validation."""

    code = "VALIDATION_ERROR"


class ExecutionError(ForgeError):
    """Raised when task or pipeline execution encounters a fatal error."""

    code = "EXECUTION_ERROR"


class AgentError(ForgeError):
    """Raised when a specialist agent fails to complete its objective."""

    code = "AGENT_ERROR"


class ToolError(ForgeError):
    """Raised when an agent tool invocation fails."""

    code = "TOOL_ERROR"


class MemoryError(ForgeError):
    """Raised when a memory read / write / retrieval operation fails."""

    code = "MEMORY_ERROR"


class IntentError(ForgeError):
    """Raised during intent parsing or clarification failures."""

    code = "INTENT_ERROR"


class SpecificationError(ForgeError):
    """Raised when specification generation or validation fails."""

    code = "SPECIFICATION_ERROR"


class ArchitectureError(ForgeError):
    """Raised during architecture generation or validation failures."""

    code = "ARCHITECTURE_ERROR"


class OrchestrationError(ForgeError):
    """Raised when the orchestration layer cannot schedule or route work."""

    code = "ORCHESTRATION_ERROR"


class DeploymentError(ForgeError):
    """Raised when a deployment pipeline step fails."""

    code = "DEPLOYMENT_ERROR"


class VerificationError(ForgeError):
    """Raised when automated verification (tests, linting, etc.) fails."""

    code = "VERIFICATION_ERROR"


class RepoIntelligenceError(ForgeError):
    """Raised by the repository-intelligence subsystem."""

    code = "REPO_INTELLIGENCE_ERROR"


class AuthenticationError(ForgeError):
    """Raised when a request cannot be authenticated."""

    code = "AUTHENTICATION_ERROR"


class AuthorizationError(ForgeError):
    """Raised when a principal lacks permission for an operation."""

    code = "AUTHORIZATION_ERROR"


class RateLimitError(ForgeError):
    """Raised when an external API or internal rate limit is exceeded."""

    code = "RATE_LIMIT_ERROR"


class NotFoundError(ForgeError):
    """Raised when a requested resource does not exist."""

    code = "NOT_FOUND_ERROR"


class ConflictError(ForgeError):
    """Raised when an operation conflicts with existing state."""

    code = "CONFLICT_ERROR"
