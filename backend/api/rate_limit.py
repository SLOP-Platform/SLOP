"""backend/api/rate_limit.py — Rate-limit configuration (step 2.4).

Per Core Rule 4.14 (lands in 2.4.e) and the strategy at
`docs/cleanup/STEP_2_4_RATE_LIMITING_STRATEGY.md`, the FastAPI surface
exposes a single `limiter` instance that route handlers decorate per
endpoint. The categorisation:

  - Heavy mutation (install/remove/replace, wizard run): 5/minute
  - Heavy read (LLM-triggering endpoints):              10/minute
  - Light mutation (settings, registry, storage):       30/minute
  - Default (everything else, mostly GETs):             60/minute

Localhost bypass: requests from `127.0.0.1`, `::1`, or pytest's
`TestClient` (which sets `client.host == "testclient"`) get a shared
key that's exempt from limits — the homelab's CLI tools (`ms-update`,
`ms-test`, the in-process health scheduler) hit the API from the
local machine and shouldn't be throttled.

Storage: in-memory (`memory://`). Single-process backend, no Redis
needed. Resets on every restart, which is acceptable since the
limiter exists primarily as a runaway-script safeguard, not a
persistent quota.

Wired in `backend/api/main.py`:

    from backend.api.rate_limit import limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _key_func(request: Request) -> str | None:
    """Bypass localhost; otherwise key on remote IP.

    Returning `None` from a slowapi key function tells slowapi to skip
    the limit entirely for this request — that's how the localhost
    bypass works (CLI tools and the in-process scheduler hit the local
    API and shouldn't be throttled).
    """
    ip = get_remote_address(request)
    if ip in _LOCAL_HOSTS:
        # slowapi accepts None as "no limit"; its advertised signature
        # `Callable[..., str]` is too narrow — see the
        # `# type: ignore[arg-type]` on the Limiter call below.
        return None
    return ip


def _is_localhost(request: Request) -> bool:
    """slowapi's `exempt_when` predicate — True means skip the limit."""
    return get_remote_address(request) in _LOCAL_HOSTS


limiter: Any = Limiter(
    key_func=_key_func,  # type: ignore[arg-type]  # _key_func returns str | None for localhost bypass
    storage_uri="memory://",
    default_limits=["60/minute"],
    # headers_enabled MUST stay False: with it True, slowapi 0.1.9's wrapper calls
    # _inject_headers() on every non-exempt request and requires the handler to
    # expose a `response: Response` param — none of our @limiter.limit handlers do,
    # so any external (non-localhost) call 500s with
    # "parameter `response` must be an instance of starlette.responses.Response".
    # The X-RateLimit-* headers can't render anyway: no SlowAPIMiddleware is installed.
    headers_enabled=False,
    swallow_errors=False,
)
