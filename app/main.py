"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api import auth, billing, health, monitors
from app.config import settings
from app.db import init_db
from app.errors import register_exception_handlers
from app.logging_conf import configure_logging, get_logger
from app.plans import PLANS

log = get_logger(__name__)
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

MAX_REQUEST_BYTES = 1_000_000


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    init_db()
    log.info(
        "app_starting",
        version=__version__, environment=settings.environment,
        billing_enabled=settings.billing_enabled, scheduler_enabled=settings.scheduler_enabled,
    )
    if settings.is_production and settings.secret_key == "dev-insecure-secret-change-me":
        raise RuntimeError("SECRET_KEY must be set to a real value in production.")

    capacity = settings.capacity_report()
    log.info("scheduler_capacity", **capacity)
    if not capacity["can_serve_one_largest_plan_customer"]:
        # Selling a plan the deployment cannot serve produces missed checks that
        # look like product failure, so say it at boot rather than discovering it
        # from a customer complaint.
        log.warning(
            "capacity_below_advertised_plan",
            message=(
                "check_batch_size / scheduler_tick_seconds is below what the largest "
                "plan promises; raise CHECK_BATCH_SIZE or lower SCHEDULER_TICK_SECONDS"
            ),
            **capacity,
        )

    scheduler_started = False
    if settings.scheduler_enabled:
        from app.core.scheduler import start_scheduler, stop_scheduler

        start_scheduler()
        scheduler_started = True
    try:
        yield
    finally:
        if scheduler_started:
            stop_scheduler()
        log.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Watchpost API",
        version=__version__,
        description=(
            "Developer-first web & API change monitoring. Declare what to watch and where "
            "to send changes; Watchpost fetches on a schedule, extracts the part you care "
            "about, and POSTs a signed, structured JSON diff when it moves."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if not settings.is_production else [settings.public_base_url],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Bind a request id to the log context and emit one access log per request."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error": {"type": "payload_too_large", "message": "Request body too large."}},
            )

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
        )
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        if not request.url.path.startswith(("/healthz", "/readyz")):
            log.info(
                "http_request",
                method=request.method, path=request.url.path,
                status_code=response.status_code, duration_ms=round(duration_ms, 2),
                user_id=getattr(request.state, "user_id", None),
            )
        return response

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(monitors.router)
    app.include_router(billing.router)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="landing.html",
            context={
                "plans": list(PLANS.values()),
                "base_url": settings.public_base_url,
                "billing_enabled": settings.billing_enabled,
                "version": __version__,
            },
        )

    def _billing_result(request: Request, ok: bool):
        return templates.TemplateResponse(
            request=request,
            name="billing_result.html",
            context={"ok": ok, "base_url": settings.public_base_url},
        )

    @app.get("/billing/success", response_class=HTMLResponse, include_in_schema=False)
    def billing_success(request: Request):
        return _billing_result(request, ok=True)

    @app.get("/billing/cancelled", response_class=HTMLResponse, include_in_schema=False)
    def billing_cancelled(request: Request):
        return _billing_result(request, ok=False)

    return app


app = create_app()
