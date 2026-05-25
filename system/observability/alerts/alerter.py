"""Alert routing and dispatch for the FORGE platform.

Architecture
------------
- ``Alert`` is the core data object.
- ``AlertChannel`` is a Protocol (structural sub-typing) — implement ``send``
  to add a new backend.
- ``AlertManager`` accepts registered channels, routes alerts by level, and
  de-duplicates noisy repeated alerts via an in-memory time-window cache.
- ``alert()`` is the top-level convenience function used throughout the system.

Built-in channels
-----------------
- ``LogAlertChannel`` — writes alerts to the structlog pipeline.
- ``SlackAlertChannel`` — POSTs a formatted message to a Slack webhook URL.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import logging
import time
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


# ========================================================================== #
# Enums & data classes
# ========================================================================== #


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Alert:
    """An alert event to be dispatched to one or more channels."""

    level: AlertLevel
    title: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> str:
        """A stable hash used for de-duplication (ignores timestamp)."""
        raw = f"{self.level}:{self.title}:{self.message}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "context": self.context,
            "timestamp": self.timestamp,
        }


# ========================================================================== #
# Channel protocol
# ========================================================================== #


@runtime_checkable
class AlertChannel(Protocol):
    """Structural interface for alert delivery backends."""

    async def send(self, alert: Alert) -> None:
        """Dispatch *alert* to this channel.

        Implementations should not raise — log internally instead.
        """
        ...


# ========================================================================== #
# Built-in channels
# ========================================================================== #


class LogAlertChannel:
    """Writes alerts to the Python logging / structlog pipeline."""

    _LEVEL_MAP = {
        AlertLevel.INFO: logging.INFO,
        AlertLevel.WARNING: logging.WARNING,
        AlertLevel.ERROR: logging.ERROR,
        AlertLevel.CRITICAL: logging.CRITICAL,
    }

    async def send(self, alert: Alert) -> None:
        level = self._LEVEL_MAP.get(alert.level, logging.INFO)
        logger.log(
            level,
            "[ALERT] %s — %s",
            alert.title,
            alert.message,
            extra={"alert_context": alert.context, "alert_level": alert.level},
        )


class SlackAlertChannel:
    """POSTs a formatted Slack Block Kit message to a webhook URL.

    Args:
        webhook_url: Slack incoming webhook URL.
        min_level: Minimum level to forward to Slack (default WARNING).
        timeout_seconds: HTTP request timeout.
    """

    _EMOJI = {
        AlertLevel.INFO: ":information_source:",
        AlertLevel.WARNING: ":warning:",
        AlertLevel.ERROR: ":x:",
        AlertLevel.CRITICAL: ":rotating_light:",
    }

    def __init__(
        self,
        webhook_url: str,
        min_level: AlertLevel = AlertLevel.WARNING,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.min_level = min_level
        self.timeout_seconds = timeout_seconds
        self._level_order = list(AlertLevel)

    def _level_index(self, level: AlertLevel) -> int:
        try:
            return self._level_order.index(level)
        except ValueError:
            return 0

    async def send(self, alert: Alert) -> None:
        if self._level_index(alert.level) < self._level_index(self.min_level):
            return

        emoji = self._EMOJI.get(alert.level, ":bell:")
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} [{alert.level.upper()}] {alert.title}",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": alert.message},
            },
        ]
        if alert.context:
            ctx_text = "\n".join(f"• *{k}*: `{v}`" for k, v in alert.context.items())
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ctx_text},
                }
            )

        payload = {"blocks": blocks}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("SlackAlertChannel failed to deliver alert: %s", exc)


class PagerDutyAlertChannel:
    """Sends CRITICAL alerts to a PagerDuty Events v2 endpoint.

    Only alerts with level ``CRITICAL`` are forwarded.

    Args:
        routing_key: PagerDuty integration routing key.
    """

    PD_URL = "https://events.pagerduty.com/v2/enqueue"

    def __init__(self, routing_key: str, timeout_seconds: float = 10.0) -> None:
        self.routing_key = routing_key
        self.timeout_seconds = timeout_seconds

    async def send(self, alert: Alert) -> None:
        if alert.level != AlertLevel.CRITICAL:
            return

        payload = {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": alert.fingerprint,
            "payload": {
                "summary": f"[FORGE] {alert.title}: {alert.message}",
                "severity": "critical",
                "source": "forge",
                "custom_details": alert.context,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(self.PD_URL, json=payload)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("PagerDutyAlertChannel failed: %s", exc)


# ========================================================================== #
# Alert manager
# ========================================================================== #


class AlertManager:
    """Routes alerts to registered channels with rate-limiting.

    Rate-limiting
    -------------
    Duplicate alerts (same fingerprint) are suppressed for
    ``dedup_window_seconds`` (default 300 s = 5 min).  A channel may still
    receive the alert if it is configured with ``bypass_dedup=True`` or the
    alert level is CRITICAL.

    Args:
        dedup_window_seconds: Suppression window for duplicate alerts.
    """

    def __init__(self, dedup_window_seconds: float = 300.0) -> None:
        self._channels: list[AlertChannel] = []
        self._dedup_window = dedup_window_seconds
        # fingerprint -> last_sent_timestamp
        self._sent_cache: dict[str, float] = {}

    def register(self, channel: AlertChannel) -> AlertManager:
        """Register a channel.  Returns self for fluent chaining."""
        self._channels.append(channel)
        return self

    def _is_duplicate(self, alert: Alert) -> bool:
        """Return True if this alert was already sent within the dedup window."""
        # CRITICAL alerts always bypass de-duplication
        if alert.level == AlertLevel.CRITICAL:
            return False
        last_sent = self._sent_cache.get(alert.fingerprint)
        if last_sent is None:
            return False
        return (time.time() - last_sent) < self._dedup_window

    async def dispatch(self, alert: Alert) -> None:
        """Send *alert* to all registered channels (concurrently).

        Duplicate alerts within the dedup window are silently dropped.
        """
        if self._is_duplicate(alert):
            logger.debug("Alert suppressed (duplicate): %s — %s", alert.level, alert.title)
            return

        self._sent_cache[alert.fingerprint] = time.time()

        tasks = [channel.send(alert) for channel in self._channels]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "AlertChannel[%d] raised during dispatch: %s",
                    i,
                    result,
                )

    def clear_dedup_cache(self) -> None:
        """Evict stale entries from the de-duplication cache."""
        now = time.time()
        expired = [fp for fp, ts in self._sent_cache.items() if (now - ts) > self._dedup_window]
        for fp in expired:
            del self._sent_cache[fp]


# ========================================================================== #
# Global manager & convenience function
# ========================================================================== #

# Default manager — populate by calling register() at application startup.
_default_manager: AlertManager = AlertManager()
# Pre-register the log channel so alerts are never silently lost
_default_manager.register(LogAlertChannel())


def get_alert_manager() -> AlertManager:
    """Return the global AlertManager instance."""
    return _default_manager


async def alert(
    level: AlertLevel,
    title: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> None:
    """Top-level convenience function for raising an alert.

    Usage::

        from system.observability.alerts.alerter import alert, AlertLevel

        await alert(AlertLevel.ERROR, "Agent failed", "Backend agent timed out", {
            "task_id": task.id,
            "agent": "backend",
        })
    """
    a = Alert(
        level=level,
        title=title,
        message=message,
        context=context or {},
    )
    await _default_manager.dispatch(a)
