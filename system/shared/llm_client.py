"""LLM client abstraction for the FORGE platform.

Provides a unified interface over multiple LLM providers.  Currently
implements ``AnthropicClient``; OpenAI support is wired in as a stub that
can be fleshed out without touching any call sites.

Usage
-----
::

    from system.shared.llm_client import get_llm_client, LLMMessage

    client = get_llm_client()
    response = await client.complete(
        messages=[LLMMessage(role="user", content="Write a FastAPI endpoint.")],
        system="You are an expert Python engineer.",
    )
    print(response.content)

    # Streaming:
    async for chunk in client.stream_complete(messages=[...]):
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from functools import lru_cache
from typing import AsyncIterator, List, Optional

import anthropic
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from system.config.settings import settings
from system.shared.constants import (
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TEMPERATURE,
    MAX_TOKENS_PER_AGENT,
    MAX_AGENT_RETRIES,
)
from system.shared.exceptions import AgentError, RateLimitError

logger = logging.getLogger(__name__)


# ========================================================================== #
# Domain types
# ========================================================================== #


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class LLMMessage:
    """A single message in a conversation turn."""

    __slots__ = ("role", "content")

    def __init__(self, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        self.role = role
        self.content = content

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}

    def __repr__(self) -> str:
        preview = self.content[:60].replace("\n", " ")
        return f"LLMMessage(role={self.role!r}, content={preview!r}...)"


class LLMResponse:
    """The structured response returned by an LLM completion call."""

    __slots__ = (
        "content",
        "model",
        "input_tokens",
        "output_tokens",
        "stop_reason",
        "latency_seconds",
    )

    def __init__(
        self,
        content: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str,
        latency_seconds: float = 0.0,
    ) -> None:
        self.content = content
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.stop_reason = stop_reason
        self.latency_seconds = latency_seconds

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __repr__(self) -> str:
        return (
            f"LLMResponse(model={self.model!r}, "
            f"tokens={self.input_tokens}+{self.output_tokens}, "
            f"stop_reason={self.stop_reason!r})"
        )


# ========================================================================== #
# Abstract base
# ========================================================================== #


class LLMClient(ABC):
    """Abstract base class for LLM provider clients."""

    @abstractmethod
    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = MAX_TOKENS_PER_AGENT,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        system: Optional[str] = None,
    ) -> LLMResponse:
        """Request a non-streaming completion and return the full response."""

    @abstractmethod
    async def complete_with_retry(
        self,
        messages: List[LLMMessage],
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = MAX_TOKENS_PER_AGENT,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        system: Optional[str] = None,
        max_attempts: int = MAX_AGENT_RETRIES,
    ) -> LLMResponse:
        """complete() wrapped with exponential back-off retry logic."""

    @abstractmethod
    async def stream_complete(
        self,
        messages: List[LLMMessage],
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = MAX_TOKENS_PER_AGENT,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        system: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Yield text chunks as they arrive from the model."""


# ========================================================================== #
# Anthropic implementation
# ========================================================================== #


class AnthropicClient(LLMClient):
    """Production Anthropic Claude client using the official SDK.

    Args:
        api_key: Anthropic API key.  Defaults to ``settings.anthropic_api_key``.
        default_model: Model identifier to use when not specified per-call.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_LLM_MODEL,
    ) -> None:
        self._api_key = api_key or settings.anthropic_api_key
        self.default_model = default_model
        self._client = anthropic.AsyncAnthropic(api_key=self._api_key)

    # ------------------------------------------------------------------ #
    # Core completion
    # ------------------------------------------------------------------ #

    async def complete(
        self,
        messages: List[LLMMessage],
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = MAX_TOKENS_PER_AGENT,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        system: Optional[str] = None,
    ) -> LLMResponse:
        """Non-streaming completion call.

        Args:
            messages: Ordered list of conversation messages.
            model: Anthropic model identifier.
            max_tokens: Maximum tokens in the completion.
            temperature: Sampling temperature (0.0 – 1.0).
            system: Optional system prompt prepended before messages.

        Returns:
            ``LLMResponse`` with content, token counts, and stop reason.

        Raises:
            RateLimitError: On HTTP 429 responses.
            AgentError: On any other API or parsing error.
        """
        kwargs: dict = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [m.to_dict() for m in messages],
        }
        if system:
            kwargs["system"] = system

        t0 = time.perf_counter()
        try:
            response: anthropic.types.Message = await self._client.messages.create(
                **kwargs
            )
        except anthropic.RateLimitError as exc:
            raise RateLimitError(
                f"Anthropic rate limit exceeded: {exc}",
                details={"model": kwargs["model"]},
            ) from exc
        except anthropic.APIError as exc:
            raise AgentError(
                f"Anthropic API error: {exc}",
                details={"model": kwargs["model"], "status": getattr(exc, "status_code", None)},
            ) from exc

        latency = time.perf_counter() - t0

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        result = LLMResponse(
            content=content,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason or "end_turn",
            latency_seconds=latency,
        )

        # Emit metrics (best-effort; never block on failure)
        try:
            from system.observability.metrics.collector import record_llm_usage  # noqa: PLC0415

            record_llm_usage(
                model=response.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_seconds=latency,
            )
        except Exception:  # noqa: BLE001
            pass

        logger.debug(
            "LLM complete",
            model=response.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            stop_reason=result.stop_reason,
            latency_s=round(latency, 3),
        )

        return result

    # ------------------------------------------------------------------ #
    # Retry wrapper
    # ------------------------------------------------------------------ #

    async def complete_with_retry(
        self,
        messages: List[LLMMessage],
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = MAX_TOKENS_PER_AGENT,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        system: Optional[str] = None,
        max_attempts: int = MAX_AGENT_RETRIES,
    ) -> LLMResponse:
        """complete() with tenacity exponential back-off.

        Retries on ``RateLimitError`` and transient ``anthropic.APIError``
        exceptions up to *max_attempts* times.

        Args:
            max_attempts: Total number of attempts (including the first).
        """
        last_exc: Optional[Exception] = None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=settings.retry_delay_seconds, min=1, max=60),
            retry=retry_if_exception_type((RateLimitError, anthropic.APIConnectionError)),
            reraise=False,
        ):
            with attempt:
                try:
                    return await self.complete(
                        messages,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        system=system,
                    )
                except (RateLimitError, anthropic.APIConnectionError) as exc:
                    last_exc = exc
                    logger.warning(
                        "LLM request failed (attempt %d/%d): %s",
                        attempt.retry_state.attempt_number,
                        max_attempts,
                        exc,
                    )
                    raise  # Let tenacity handle retry

        raise AgentError(
            f"LLM call failed after {max_attempts} attempts",
            details={"last_error": str(last_exc)},
        )

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #

    async def stream_complete(
        self,
        messages: List[LLMMessage],
        *,
        model: str = DEFAULT_LLM_MODEL,
        max_tokens: int = MAX_TOKENS_PER_AGENT,
        temperature: float = DEFAULT_LLM_TEMPERATURE,
        system: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Yield text chunks as they arrive from the Anthropic streaming API.

        Usage::

            async for chunk in client.stream_complete(messages):
                print(chunk, end="", flush=True)
        """
        kwargs: dict = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [m.to_dict() for m in messages],
        }
        if system:
            kwargs["system"] = system

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text_chunk in stream.text_stream:
                    yield text_chunk
        except anthropic.RateLimitError as exc:
            raise RateLimitError(f"Anthropic rate limit during streaming: {exc}") from exc
        except anthropic.APIError as exc:
            raise AgentError(f"Anthropic API error during streaming: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Utility
    # ------------------------------------------------------------------ #

    async def count_tokens(self, messages: List[LLMMessage], system: Optional[str] = None) -> int:
        """Estimate token count for a message list without sending a completion.

        Uses the Anthropic token-counting endpoint when available; falls back
        to a rough character-based estimate.
        """
        try:
            kwargs: dict = {
                "model": self.default_model,
                "messages": [m.to_dict() for m in messages],
            }
            if system:
                kwargs["system"] = system
            result = await self._client.messages.count_tokens(**kwargs)
            return result.input_tokens
        except Exception:  # noqa: BLE001
            # Fallback: ~4 chars per token
            total_chars = sum(len(m.content) for m in messages)
            if system:
                total_chars += len(system)
            return total_chars // 4


# ========================================================================== #
# Singleton factory
# ========================================================================== #


@lru_cache(maxsize=1)
def get_llm_client() -> AnthropicClient:
    """Return the application-wide LLM client singleton.

    The instance is created on first call and cached for the process lifetime.
    """
    client = AnthropicClient(
        api_key=settings.anthropic_api_key,
        default_model=DEFAULT_LLM_MODEL,
    )
    logger.info("AnthropicClient initialised (model=%s)", client.default_model)
    return client
