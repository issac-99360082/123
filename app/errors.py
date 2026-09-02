"""Uniform JSON error responses.

Every failure the API returns has the same shape:
    {"error": {"type": "...", "message": "...", "detail": ...}}
so clients can branch on `type` without parsing prose.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_conf import get_logger

log = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, client-facing failures."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "bad_request"

    def __init__(self, message: str, detail: object | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "unauthorized"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "forbidden"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not_found"


class QuotaExceededError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    error_type = "quota_exceeded"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_type = "rate_limited"


class ValidationFailedError(AppError):
    status_code = 422
    error_type = "validation_failed"


class UpstreamUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = "service_unavailable"


def _payload(error_type: str, message: str, detail: object | None = None) -> dict:
    body: dict = {"error": {"type": error_type, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.error_type, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        # Pydantic puts the original exception object in `ctx`, which json.dumps
        # cannot serialise - encoding it would turn every custom-validator
        # failure into a 500. Drop `ctx` and encode the rest defensively.
        details = [
            {k: v for k, v in error.items() if k != "ctx"} for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                _payload("validation_failed", "Request body failed validation.", details)
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        mapping = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(mapping.get(exc.status_code, "http_error"), str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Never leak internals to the caller; log the full trace with a
        # reference the user can quote in a support request.
        ref = uuid.uuid4().hex[:12]
        log.exception(
            "unhandled_exception", ref=ref, path=request.url.path, method=request.method,
            error=exc.__class__.__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", f"Unexpected error. Reference: {ref}"),
        )
