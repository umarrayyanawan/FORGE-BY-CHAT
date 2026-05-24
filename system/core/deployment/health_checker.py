"""Health checker — probes HTTP endpoints and aggregates service health."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from system.core.deployment.schemas import HealthStatus
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class HealthChecker:
    """Performs HTTP health checks against deployed services.

    Args:
        http_client: An ``httpx.AsyncClient`` instance.  A default client with
            a 10-second timeout is created if none is provided.  Callers that
            manage their own client lifecycle should pass one in and close it
            themselves.
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._owned_client = http_client is None
        self.client: httpx.AsyncClient = http_client or httpx.AsyncClient(timeout=10.0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owned_client:
            await self.client.aclose()

    async def __aenter__(self) -> "HealthChecker":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Core check
    # ------------------------------------------------------------------

    async def check(
        self,
        url: str,
        expected_status: int = 200,
        timeout: int = 10,
    ) -> HealthStatus:
        """Probe *url* and return a :class:`HealthStatus` describing the result."""
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self.client.get(url),
                timeout=float(timeout),
            )
            response_time_ms = int((time.monotonic() - start) * 1000)
            healthy = response.status_code == expected_status
            details: Dict[str, Any] = {
                "url": url,
                "status_code": response.status_code,
            }
            # Include a snippet of the response body on failure to aid diagnosis.
            if not healthy:
                try:
                    details["body_snippet"] = response.text[:200]
                except Exception:
                    pass
            logger.debug(
                "Health check",
                url=url,
                healthy=healthy,
                status_code=response.status_code,
                response_time_ms=response_time_ms,
            )
            return HealthStatus(
                healthy=healthy,
                status_code=response.status_code,
                response_time_ms=response_time_ms,
                details=details,
            )
        except asyncio.TimeoutError:
            response_time_ms = int((time.monotonic() - start) * 1000)
            logger.warning("Health check timed out", url=url, timeout=timeout)
            return HealthStatus(
                healthy=False,
                status_code=0,
                response_time_ms=response_time_ms,
                details={"url": url, "error": f"Timed out after {timeout}s"},
            )
        except httpx.RequestError as exc:
            response_time_ms = int((time.monotonic() - start) * 1000)
            logger.warning("Health check request error", url=url, error=str(exc))
            return HealthStatus(
                healthy=False,
                status_code=0,
                response_time_ms=response_time_ms,
                details={"url": url, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Retry helpers
    # ------------------------------------------------------------------

    async def check_repeatedly(
        self,
        url: str,
        retries: int = 10,
        interval: int = 5,
    ) -> bool:
        """Probe *url* up to *retries* times, sleeping *interval* seconds between attempts.

        Returns ``True`` as soon as a healthy response is received, or ``False``
        if all attempts fail.
        """
        for attempt in range(1, retries + 1):
            status = await self.check(url)
            if status.healthy:
                logger.info(
                    "Service became healthy",
                    url=url,
                    attempt=attempt,
                )
                return True
            logger.info(
                "Health check failed — will retry",
                url=url,
                attempt=attempt,
                total_retries=retries,
                status_code=status.status_code,
                error=status.details.get("error"),
            )
            if attempt < retries:
                await asyncio.sleep(interval)
        logger.warning(
            "Health check exhausted retries",
            url=url,
            retries=retries,
        )
        return False

    async def wait_for_healthy(
        self,
        url: str,
        timeout_seconds: int = 300,
        interval: int = 5,
    ) -> bool:
        """Poll *url* until healthy or *timeout_seconds* has elapsed.

        The interval between probes is *interval* seconds (default 5).
        """
        retries = max(1, timeout_seconds // interval)
        return await self.check_repeatedly(url, retries=retries, interval=interval)

    # ------------------------------------------------------------------
    # Bulk check
    # ------------------------------------------------------------------

    async def check_all_services(
        self,
        deployment_id: str,
        services: List[Dict[str, Any]],
    ) -> Dict[str, HealthStatus]:
        """Check every service in *services* concurrently.

        Each entry in *services* should have the keys:
        - ``name``         — logical service name used as the result key.
        - ``health_url``   — URL to probe.
        - ``expected_status`` (optional, default 200).

        Returns a mapping of service name → :class:`HealthStatus`.
        """
        async def _check_one(svc: Dict[str, Any]) -> tuple[str, HealthStatus]:
            name = svc.get("name", "unknown")
            url = svc.get("health_url", "")
            if not url:
                return name, HealthStatus(
                    healthy=False,
                    status_code=0,
                    details={"error": "No health_url configured"},
                )
            expected = svc.get("expected_status", 200)
            status = await self.check(url, expected_status=expected)
            return name, status

        results_list = await asyncio.gather(
            *[_check_one(svc) for svc in services],
            return_exceptions=False,
        )
        results: Dict[str, HealthStatus] = dict(results_list)  # type: ignore[arg-type]
        healthy_count = sum(1 for s in results.values() if s.healthy)
        logger.info(
            "Bulk health check complete",
            deployment_id=deployment_id,
            total=len(results),
            healthy=healthy_count,
        )
        return results

    async def assert_healthy(
        self,
        url: str,
        timeout_seconds: int = 300,
        interval: int = 5,
    ) -> None:
        """Like :meth:`wait_for_healthy` but raises ``RuntimeError`` on failure."""
        ok = await self.wait_for_healthy(url, timeout_seconds=timeout_seconds, interval=interval)
        if not ok:
            raise RuntimeError(
                f"Service at {url!r} did not become healthy within {timeout_seconds}s"
            )
