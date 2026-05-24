import time
from fastapi import Request, HTTPException, status
from system.observability.logging.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    def __init__(self, redis, max_requests: int = 100, window_seconds: int = 60):
        self.redis = redis
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    async def check(self, identifier: str) -> bool:
        """Sliding window rate limit. Returns True if allowed."""
        key = f"FORGE:RATELIMIT:{identifier}"
        now = time.time()
        window_start = now - self.window_seconds
        pipeline = self.redis.pipeline()
        pipeline.zremrangebyscore(key, 0, window_start)
        pipeline.zadd(key, {str(now): now})
        pipeline.zcard(key)
        pipeline.expire(key, self.window_seconds * 2)
        results = await pipeline.execute()
        count = results[2]
        return count <= self.max_requests

    async def remaining(self, identifier: str) -> int:
        """Return how many requests remain in the current window."""
        key = f"FORGE:RATELIMIT:{identifier}"
        now = time.time()
        window_start = now - self.window_seconds
        await self.redis.zremrangebyscore(key, 0, window_start)
        count = await self.redis.zcard(key)
        return max(0, self.max_requests - count)

    async def reset(self, identifier: str) -> None:
        """Reset the rate limit counter for an identifier (e.g. admin override)."""
        key = f"FORGE:RATELIMIT:{identifier}"
        await self.redis.delete(key)
        logger.info("Rate limit reset", identifier=identifier)


RATE_LIMITS = {
    "default": (100, 60),
    "llm": (10, 60),
    "deploy": (5, 60),
    "pipeline": (20, 60),
}


def _get_limit_for_path(path: str) -> tuple[int, int]:
    """Return (max_requests, window_seconds) for a given request path."""
    if "/pipeline" in path:
        return RATE_LIMITS["pipeline"]
    if "/deploy" in path:
        return RATE_LIMITS["deploy"]
    if "/llm" in path or "/generate" in path:
        return RATE_LIMITS["llm"]
    return RATE_LIMITS["default"]


async def rate_limit_dependency(request: Request) -> bool:
    """
    FastAPI dependency for rate limiting.

    In production, wire up Redis via app.state.redis and use auth user ID
    as the identifier when available. Falls back to client IP.
    """
    client_ip = request.client.host if request.client else "unknown"

    # Prefer authenticated user ID when available (populated by JWT middleware)
    identifier = getattr(request.state, "user_id", None) or client_ip

    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        # Redis not configured — allow all requests but log a warning
        logger.warning("Redis not configured, rate limiting disabled")
        return True

    max_requests, window_seconds = _get_limit_for_path(request.url.path)
    limiter = RateLimiter(redis, max_requests=max_requests, window_seconds=window_seconds)

    allowed = await limiter.check(identifier)
    if not allowed:
        remaining = 0
        retry_after = window_seconds
        logger.warning(
            "Rate limit exceeded",
            identifier=identifier,
            path=request.url.path,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "message": f"Too many requests. Max {max_requests} per {window_seconds}s.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after), "X-RateLimit-Remaining": str(remaining)},
        )

    remaining = await limiter.remaining(identifier)
    # Attach remaining to request state so response middleware can add headers
    request.state.rate_limit_remaining = remaining
    return True
