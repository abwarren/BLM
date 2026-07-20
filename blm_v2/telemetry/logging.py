"""
BLM V2 — Structured Logging Setup

Configures structlog for the BLM platform with:
  - JSON logging for production environments
  - Console-friendly coloured output for development
  - Correlation IDs threaded through every log event per request
  - Standardised log level control via LOG_LEVEL env var

Usage:
    from blm_v2.telemetry.logging import setup_logging, get_logger

    setup_logging(environment="development")
    logger = get_logger(__name__)
    logger.info("server_started", port=8000, version="2.0.0")
"""

import logging
import os
import sys
import uuid
import contextvars
from typing import Optional

import structlog
from structlog.processors import JSONRenderer, TimeStamper, StackInfoRenderer
from structlog.stdlib import ProcessorFormatter, BoundLogger

# ── Correlation ID context ────────────────────────────────────────────

correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def _add_correlation_id(logger: structlog.BoundLogger, method_name: str, event_dict: dict) -> dict:
    """Inject the current request's correlation ID into every log event."""
    cid = correlation_id_ctx.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


# ── Log level from environment ───────────────────────────────────────

def _resolve_log_level() -> str:
    return os.environ.get("LOG_LEVEL", "INFO").upper()


# ── Processor pipeline builders ──────────────────────────────────────

def _shared_processors(timestamp: bool = True) -> list:
    """Common processors shared by both dev and prod pipelines."""
    procs = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        StackInfoRenderer(),
        structlog.dev.set_exc_info,
        _add_correlation_id,
        structlog.processors.UnicodeDecoder(),
    ]
    if timestamp:
        procs.insert(0, TimeStamper(fmt="iso", utc=True))
    return procs


def _production_pipeline() -> dict:
    """JSON logging — ideal for containerised/cloud deployments."""
    pre_chain = _shared_processors(timestamp=True)

    return {
        "wrapper_class": structlog.stdlib.BoundLogger,
        "context_class": dict,
        "logger_factory": structlog.stdlib.LoggerFactory(),
        "cache_logger_on_first_use": True,
        "processors": [
            structlog.stdlib.filter_by_level,
            *_shared_processors(timestamp=True),
            ProcessorFormatter.wrap_for_formatter,
        ],
        "foreign_pre_chain": pre_chain,
    }


def _development_pipeline() -> dict:
    """Colourised console logging with readable timestamps."""
    return {
        "wrapper_class": structlog.stdlib.BoundLogger,
        "context_class": dict,
        "logger_factory": structlog.stdlib.LoggerFactory(),
        "cache_logger_on_first_use": True,
        "processors": [
            structlog.stdlib.filter_by_level,
            *_shared_processors(timestamp=True),
            structlog.dev.ConsoleRenderer(
                colors=True,
                sort_keys=False,
            ),
        ],
    }


# ── Public API ────────────────────────────────────────────────────────

def setup_logging(
    environment: Optional[str] = None,
    log_level: Optional[str] = None,
) -> None:
    """Configure the structlog logging pipeline.

    Parameters
    ----------
    environment : str, optional
        ``"production"`` enables JSON output; ``"development"`` (or anything
        else) enables colourised console output.  Falls back to the
        ``BLM_ENV`` or ``ENVIRONMENT`` env vars, then ``"development"``.
    log_level : str, optional
        Override the minimum log level.  Falls back to ``LOG_LEVEL`` env var,
        then ``"INFO"``.
    """
    env = environment or os.environ.get("BLM_ENV") or os.environ.get("ENVIRONMENT") or "development"
    level = (log_level or _resolve_log_level()).upper()

    if env.lower() in ("production", "prod"):
        pipeline = _production_pipeline()
    else:
        pipeline = _development_pipeline()

    structlog.configure(**pipeline)

    # Wire structlog into the stdlib logging system so that third-party
    # libraries (fastapi, uvicorn, httpx, etc.) also get structured output.
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    # ProcessorFormatter always needs a processor or processors list.
    # Production → JSON; Development → plain-text with timestamps.
    stdlib_processors = _shared_processors(timestamp=True)
    if env.lower() in ("production", "prod"):
        stdlib_processors.append(JSONRenderer())
    else:
        stdlib_processors.append(
            structlog.dev.ConsoleRenderer(colors=False, sort_keys=False)
        )

    formatter = ProcessorFormatter(
        processors=stdlib_processors,
        foreign_pre_chain=_shared_processors(timestamp=True),
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any pre-existing handlers (e.g. uvicorn defaults)
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for name in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: Optional[str] = None) -> BoundLogger:
    """Return a structlog ``BoundLogger`` for the given *name*.

    If *name* is ``None`` (the default) the caller's module name is
    inferred automatically.
    """
    return structlog.get_logger(name or __name__)


def generate_correlation_id() -> str:
    """Return a short, unique correlation ID (12 hex chars)."""
    return uuid.uuid4().hex[:12]


class CorrelationIdMiddleware:
    """ASGI middleware that injects a correlation ID into the context var.

    Install on a FastAPI app::

        app.add_middleware(CorrelationIdMiddleware)

    Every request then gets a ``correlation_id`` that flows into structlog
    log events.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Use an existing header or generate a new ID
        headers = dict(scope.get("headers", []) or [])
        raw = headers.get(b"x-correlation-id", b"")
        cid = raw.decode("utf-8") if raw else generate_correlation_id()

        token = correlation_id_ctx.set(cid)
        try:
            await self.app(scope, receive, send)
        finally:
            correlation_id_ctx.reset(token)
