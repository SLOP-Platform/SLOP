"""Safe HTTP error detail — log the exception server-side, return a static client message.

CodeQL `py/stack-trace-exposure`: interpolating a caught exception into an
``HTTPException`` detail (``str(e)``, ``f"...{e}"``) leaks internal state — absolute
filesystem paths, library-internal messages, stack context — to the API client. That is
both an information-disclosure risk and a UX smell (raw tracebacks are not user guidance).

Route every such raise through :func:`safe_detail`. The exception is logged server-side
with full context (for operators), and only a static, caller-authored message crosses the
API boundary. Because the returned value is the caller's constant ``public`` string — never
the exception text — the taint path from exception to HTTP response is broken.

Usage::

    from backend.core.error_detail import safe_detail

    try:
        ...
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=safe_detail(e, "Could not read directory", log=log),
        ) from e

The ``from e`` chaining is preserved (server-side traceback context); the client sees only
``"Could not read directory"``.
"""

from __future__ import annotations

from typing import Any


def safe_detail(exc: BaseException, public: str, *, log: Any) -> str:
    """Log ``exc`` server-side and return the static ``public`` message for the client.

    Args:
        exc: The caught exception. Logged at ERROR with full traceback; NEVER returned.
        public: A static, caller-authored, client-safe message. This exact string is
            returned — it must not embed ``exc`` or any internal/sensitive value.
        log: The module's structlog logger (``log.exception`` is used).

    Returns:
        ``public`` verbatim — safe to hand to ``HTTPException(detail=...)``.
    """
    # structlog .exception() logs at ERROR and attaches the active exception's traceback
    # (we are always called from within an ``except`` block). Full context stays on the
    # server; only ``public`` is returned to the caller for the client response.
    log.exception(public, error=str(exc), error_type=type(exc).__name__)
    return public
