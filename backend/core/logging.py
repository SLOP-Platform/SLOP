"""backend/core/logging.py — Structured logging configuration (step 2.3).

Per Core Rule 4.13 (lands in 2.3.h) and the strategy at
`docs/cleanup/STEP_2_3_STRUCTURED_LOGGING_STRATEGY.md`, every backend log
event flows through `structlog` with a stable schema:

    {
      "timestamp":      "2026-05-08T17:23:01.234Z",   # ISO 8601 UTC
      "level":          "info",
      "logger":         "backend.health.checker",
      "subsystem":      "health",                     # derived from logger
      "correlation_id": "7e2c3a4b-...",                # request scope
      "event":          "health cycle complete",
      ...                                              # event-specific kwargs
    }

Output format is selected by `MS_LOG_FORMAT`:
  - `json`    — machine-readable JSON for production log shippers.
  - `console` — human-readable key-value with ANSI colors (default).

Log level is selected by `MS_LOG_LEVEL` (default INFO).

**Stdlib bridge.** `structlog.stdlib.ProcessorFormatter` is wired so that
stdlib `logging` calls (any `log = logging.getLogger(__name__)` site that
hasn't been migrated to `get_logger` yet) ALSO flow through the same
processor chain — meaning third-party libraries and not-yet-swept backend
modules all emit the same 6-field schema during the 2.3.e migration. Once
the sweep completes the bridge becomes redundant but harmless; we keep it
to support third-party libraries forever.

Usage from any backend module:

    from backend.core.logging import get_logger
    log = get_logger(__name__)
    log.info("event name", key1=value1, key2=value2)

Correlation IDs propagate via Python `contextvars`. The middleware in
`backend.api.middleware.CorrelationIdMiddleware` sets the contextvar at
the entry of every HTTP request; nested code (sync or async) inherits
it automatically. Non-API contexts (scheduler ticks, CLI invocations)
should `set_correlation_id(...)` themselves at their entry point.
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar, Token
from typing import Any

import structlog


# ── Correlation ID context ───────────────────────────────────────────


_correlation_id: ContextVar[str] = ContextVar(
    "correlation_id",
    default="(no-correlation)",
)


def set_correlation_id(value: str) -> Token[str]:
    """Set the current correlation ID; returns a token for `reset_correlation_id`."""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str]) -> None:
    """Reset the correlation ID using the token from `set_correlation_id`."""
    _correlation_id.reset(token)


def get_correlation_id() -> str:
    """Return the current correlation ID, or the no-correlation sentinel."""
    return _correlation_id.get()


# ── structlog processors ─────────────────────────────────────────────


def _add_correlation_id(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Inject the current contextvar correlation ID into the event."""
    event_dict["correlation_id"] = _correlation_id.get()
    return event_dict


def _add_subsystem(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Derive `subsystem` from the logger name (first segment after `backend.`)."""
    logger_name = event_dict.get("logger") or ""
    if logger_name.startswith("backend."):
        parts = logger_name.split(".")
        if len(parts) >= 2:
            event_dict["subsystem"] = parts[1]
    return event_dict


# ── Configuration ────────────────────────────────────────────────────


_configured = False


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Configure structlog + the stdlib-bridge handler. Idempotent.

    Stdlib log records (from un-migrated modules and third-party libraries)
    flow through the same processor chain as structlog records via
    `ProcessorFormatter`. Both kinds emit the 6-field schema with the same
    renderer, so the output is uniform regardless of which logging API the
    call site used.

    Args:
        level: log level name (DEBUG / INFO / WARNING / ERROR / CRITICAL).
            Defaults to MS_LOG_LEVEL env var, then INFO.
        fmt:  output format ("json" or "console"). Defaults to
            MS_LOG_FORMAT env var, then "console".
    """
    global _configured
    level_str = (level or os.environ.get("MS_LOG_LEVEL", "INFO")).upper()
    fmt_str = (fmt or os.environ.get("MS_LOG_FORMAT", "console")).lower()
    level_int = getattr(logging, level_str, logging.INFO)

    # Final renderer — JSON for production, key-value for dev.
    renderer: Any
    if fmt_str == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Pre-render processor chain — runs for BOTH structlog and stdlib records.
    pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.stdlib.add_logger_name,
        _add_correlation_id,
        _add_subsystem,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Configure structlog itself. The chain ends with `wrap_for_formatter`
    # which hands off the event_dict to ProcessorFormatter for rendering.
    structlog.configure(
        processors=[
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,  # so repeated configure() actually re-binds
    )

    # Configure the stdlib root handler. ProcessorFormatter renders
    # stdlib records that came in WITHOUT structlog (foreign_pre_chain
    # prepares them) AND structlog records that came in via the
    # `wrap_for_formatter` handoff.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_int)

    _configured = True


def get_logger(name: str) -> Any:
    """Return a structlog BoundLogger. Auto-configures on first call."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
