"""FORGE API Gateway — production FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import time
from typing import Any
import uuid

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from system.agents.router import router as agents_router
from system.api.auth.router import router as auth_router
from system.api.orchestration_api.router import router as pipeline_router
from system.config.settings import settings
from system.core.deployment.router import router as deployment_router
from system.core.evolution.router import router as evolution_router

# ---------------------------------------------------------------------------
# Routers — imported last to avoid circular imports during startup
# ---------------------------------------------------------------------------
from system.core.intent.router import router as intent_router
from system.core.memory.router import router as memory_router
from system.core.monitoring.router import router as monitoring_router
from system.core.orchestration.graph_router import router as task_graph_router
from system.core.orchestration.orchestration_router import router as orchestration_router
from system.core.planning.router import router as planning_router
from system.core.specification.router import router as spec_router
from system.core.verification.router import router as verification_router
from system.observability.logging.logger import get_logger, setup_logging
from system.repo_intelligence.router import router as intelligence_router
from system.shared.database import check_db_connection, init_db
from system.shared.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ForgeError,
    NotFoundError,
    RateLimitError,
)
from system.shared.redis_client import get_redis
from system.shared.schemas import ErrorResponse, HealthCheckResponse
from system.tools.router import router as tools_router

logger = get_logger(__name__)


# ============================================================================
# Lifespan
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup → serve → shutdown."""
    # ── Startup ──────────────────────────────────────────────────────────────
    setup_logging()
    logger.info("FORGE API starting up", version=settings.version, debug=settings.debug)

    # Verify database connectivity
    db_ok = await check_db_connection()
    if not db_ok:
        logger.warning("Database is not reachable on startup — migrations may be pending")

    # In development, auto-create tables; production relies on Alembic.
    if settings.debug:
        await init_db()
        logger.info("Development mode: tables ensured via init_db()")

    logger.info("FORGE API ready", version=settings.version)
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("FORGE API shutting down")


# ============================================================================
# Application factory
# ============================================================================


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="FORGE API",
        description=(
            "Autonomous Software Production System — drives the full pipeline from "
            "natural-language intent through specification, architecture, code generation, "
            "verification, and deployment."
        ),
        version=settings.version,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
        terms_of_service="https://forge.ai/terms",
        contact={"name": "FORGE Team", "email": "team@forge.ai"},
        license_info={"name": "Proprietary"},
    )

    # ── Middleware (order matters — outermost runs first on request) ──────────
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Process-Time"],
    )
    application.add_middleware(GZipMiddleware, minimum_size=1000)

    # ── Custom middleware ─────────────────────────────────────────────────────
    @application.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        """Attach a unique X-Request-ID to every request/response."""
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @application.middleware("http")
    async def timing_middleware(request: Request, call_next: Any) -> Response:
        """Record per-request wall-clock time and expose as X-Process-Time."""
        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Process-Time"] = f"{duration_ms:.2f}ms"
        return response

    @application.middleware("http")
    async def logging_middleware(request: Request, call_next: Any) -> Response:
        """Structured request/response logging for every HTTP transaction."""
        request_id = getattr(request.state, "request_id", "-")
        log = logger.bind(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client=request.client.host if request.client else "unknown",
        )
        log.info("request.started")
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000
            log.info(
                "request.completed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
            return response
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            log.exception(
                "request.failed",
                error=str(exc),
                duration_ms=round(duration_ms, 2),
            )
            raise

    # ── Exception handlers ────────────────────────────────────────────────────
    @application.exception_handler(ForgeError)
    async def forge_error_handler(request: Request, exc: ForgeError) -> JSONResponse:
        """Map FORGE domain errors to structured HTTP responses."""
        request_id = getattr(request.state, "request_id", None)

        # Choose HTTP status from exception type
        if isinstance(exc, AuthenticationError):
            http_status = status.HTTP_401_UNAUTHORIZED
        elif isinstance(exc, AuthorizationError):
            http_status = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, NotFoundError):
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, RateLimitError):
            http_status = status.HTTP_429_TOO_MANY_REQUESTS
        else:
            http_status = status.HTTP_400_BAD_REQUEST

        logger.warning(
            "forge_error",
            code=exc.code,
            message=exc.message,
            details=exc.details,
            request_id=request_id,
            status_code=http_status,
        )

        body = ErrorResponse(
            error=exc.message,
            code=exc.code,
            details=exc.details,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=http_status,
            content=body.model_dump(),
            headers={"X-Request-ID": request_id or ""},
        )

    @application.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all for unexpected exceptions — return 500 without leaking internals."""
        request_id = getattr(request.state, "request_id", None)
        logger.exception(
            "unhandled_exception",
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            request_id=request_id,
        )
        body = ErrorResponse(
            error="An internal server error occurred. Please try again later.",
            code="INTERNAL_SERVER_ERROR",
            request_id=request_id,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=body.model_dump(),
            headers={"X-Request-ID": request_id or ""},
        )

    # ── System routes ─────────────────────────────────────────────────────────
    @application.get(
        "/health",
        response_model=HealthCheckResponse,
        tags=["system"],
        summary="Health check",
        description="Returns the overall system health and per-component status.",
    )
    async def health() -> HealthCheckResponse:
        components: dict[str, str] = {}

        # Database
        try:
            db_ok = await check_db_connection()
            components["database"] = "ok" if db_ok else "degraded"
        except Exception:
            components["database"] = "error"

        # Redis
        try:
            redis = await get_redis()
            await redis.ping()
            components["redis"] = "ok"
        except Exception:
            components["redis"] = "error"

        overall = "ok" if all(v == "ok" for v in components.values()) else "degraded"
        return HealthCheckResponse(
            status=overall,
            version=settings.version,
            components=components,
        )

    @application.get(
        "/metrics",
        tags=["system"],
        summary="Prometheus metrics",
        description="Exposes application metrics in Prometheus text format.",
        response_class=PlainTextResponse,
    )
    async def metrics() -> PlainTextResponse:
        """Expose basic Prometheus-compatible metrics.

        In production, wire this to prometheus_client's generate_latest().
        """
        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

            data = generate_latest()
            return PlainTextResponse(content=data, media_type=CONTENT_TYPE_LATEST)
        except ImportError:
            # prometheus_client not installed — return minimal stub
            metric_lines = [
                "# HELP forge_up Whether the FORGE API is running",
                "# TYPE forge_up gauge",
                "forge_up 1",
                "# HELP forge_version_info Version info for FORGE",
                "# TYPE forge_version_info gauge",
                f'forge_version_info{{version="{settings.version}"}} 1',
            ]
            return PlainTextResponse(
                content="\n".join(metric_lines) + "\n",
                media_type="text/plain; version=0.0.4",
            )

    # ── Routers ───────────────────────────────────────────────────────────────
    prefix = "/api/v1"

    application.include_router(auth_router, prefix=prefix)
    application.include_router(intent_router, prefix=prefix)
    application.include_router(spec_router, prefix=prefix)
    application.include_router(planning_router, prefix=prefix)
    application.include_router(task_graph_router, prefix=prefix)
    application.include_router(orchestration_router, prefix=prefix)
    application.include_router(agents_router, prefix=prefix)
    application.include_router(intelligence_router, prefix=prefix)
    application.include_router(tools_router, prefix=prefix)
    application.include_router(verification_router, prefix=prefix)
    application.include_router(deployment_router, prefix=prefix)
    application.include_router(memory_router, prefix=prefix)
    application.include_router(evolution_router, prefix=prefix)
    application.include_router(pipeline_router, prefix=prefix)
    application.include_router(monitoring_router, prefix=prefix)

    return application


app = create_app()
