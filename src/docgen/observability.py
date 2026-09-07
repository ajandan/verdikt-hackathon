"""Structured logging setup — §12 audit-grade posture.

JSON logs to stdout; timestamps in UTC; request id propagated via contextvars so
`log.info(...)` inside any handler is stamped with the current request.
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from time import perf_counter

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_request_id(_logger, _name, event_dict):
    rid = _request_id.get()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Per-request access log with a generated request_id."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = _request_id.set(rid)
        log = structlog.get_logger("docgen.request")
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request_unhandled_error",
                method=request.method,
                path=request.url.path,
                duration_ms=int((perf_counter() - started) * 1000),
            )
            _request_id.reset(token)
            raise
        response.headers["x-request-id"] = rid
        log.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        _request_id.reset(token)
        return response
