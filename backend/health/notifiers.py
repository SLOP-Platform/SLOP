"""backend/health/notifiers.py

Thin notification-provider registry (#989).

The health checker and the agent audit path both deliver push notifications
through a single chokepoint (``checker._send_notification``). Historically that
chokepoint spoke *only* ntfy — a hardcoded HTTP POST. This module extracts the
delivery behind a small provider interface so an operator can route
notifications to an alternate backend (Gotify today; Discord / SMTP / Telegram
are future providers) by setting the ``notifier_provider`` setting.

This is a **thin slot** in the sense of #989. A notifier has no container, no
deploy/remove lifecycle, and no state to migrate, so it deliberately does NOT
join the heavy ``backend/infra/slots.py`` ``SLOT_CONTRACTS`` framework — that
framework models swappable *container infra* via ``InfraProvider`` (deploy /
remove / verify, Traefik wiring, a swap engine, C8/C9 migration conformance),
none of which a fire-and-forget notifier has. It is a lightweight parallel
registry with one job: ``send(title, message, priority) -> bool``.

Selection is **fail-closed**: an *unknown* configured provider name does not
silently fall back to ntfy (that would hide a misconfiguration and misroute
alerts) — it logs an error and reports failure. An *unset* provider defaults to
ntfy, preserving the established behavior exactly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

from backend.core.logging import get_logger
from backend.core.url_guard import UrlNotAllowed, assert_not_metadata_url
from backend.core.url_guard_httpx import pinned_async_client

log = get_logger(__name__)


def _egress_allowed(provider: str, url: str) -> bool:
    """SSRF-floor pre-flight for an operator-configured notifier URL (#1193).

    Notifier URLs are operator-configured, so a malicious/mis-set value could aim the
    server's outbound POST at a cloud-metadata endpoint (169.254.169.254) or a
    link-local address — classic SSRF against a self-hosted box's instance metadata.
    Returns ``False`` (and logs) when ``url``'s host is LITERALLY such an address; a
    private-LAN / ``localhost`` notifier (the common self-hosted case, incl. the
    default ``http://ntfy:80``) is deliberately ALLOWED — the floor blocks only the
    policy-free always-deny set, not general private IPs (the broader policy stays
    #1193 DToC).

    Scope (honest): ``resolve_dns=False`` — this is a LITERAL-host floor. A notifier
    URL is operator-set (not per-request attacker input), so the realistic vector is a
    literal metadata IP, not DNS-rebind; and httpx re-resolves at connect time anyway,
    so a pre-flight resolve would only be layer-1 with a TOCTOU window. Full DNS-rebind
    + connect-time closure (a pinning httpx transport, reused across all httpx user-URL
    sites) is the larger seam tracked in #1193. ``assert_not_metadata_url`` keeps the
    resolve leg (default on) for the user-URL sites that need it."""
    try:
        assert_not_metadata_url(url, resolve_dns=False)
    except UrlNotAllowed as e:
        log.warning("%s notification blocked (SSRF floor): %s", provider, e)
        return False
    return True


# Default ntfy connection — matches the historical hardcoded defaults in
# checker._send_notification and the api/platform.py wizard fields.
DEFAULT_NTFY_URL = "http://ntfy:80"
DEFAULT_NTFY_TOPIC = "slop"

# The settings key that selects the active provider. Unset -> "ntfy".
PROVIDER_SETTING_KEY = "notifier_provider"
DEFAULT_PROVIDER = "ntfy"

# Settings keys this module may read (kept narrow so the defensive load is cheap).
_SETTING_KEYS = (
    PROVIDER_SETTING_KEY,
    "ntfy_url",
    "ntfy_topic",
    "gotify_url",
    "gotify_token",
)


class Notifier(ABC):
    """A push-notification delivery backend.

    Concrete notifiers MUST NOT raise from ``send``: a delivery failure is a
    ``False`` return. Callers — health failure alerts and the agent
    kill-switch audit — must never crash because a notification could not be
    delivered.
    """

    #: Stable provider id (matches the registry key and the setting value).
    name: str = "notifier"

    @abstractmethod
    async def send(self, title: str, message: str, priority: str = "default") -> bool:
        """Deliver one notification. Returns True iff it was accepted."""
        raise NotImplementedError


class NtfyNotifier(Notifier):
    """ntfy.sh-style notifier — the historical default delivery path.

    Behavior is ported verbatim from ``checker._send_notification`` so the
    default provider is byte-for-byte equivalent to the pre-#989 code.
    """

    name = "ntfy"

    def __init__(self, url: str = DEFAULT_NTFY_URL, topic: str = DEFAULT_NTFY_TOPIC) -> None:
        self._url = url
        self._topic = topic

    async def send(self, title: str, message: str, priority: str = "default") -> bool:
        target = f"{self._url}/{self._topic}"
        if not _egress_allowed(self.name, target):
            return False
        try:
            async with pinned_async_client(timeout=5) as client:
                resp = await client.post(
                    target,
                    content=message.encode(),
                    headers={
                        "Title": title,
                        "Priority": priority,
                        "Tags": "warning" if priority != "urgent" else "rotating_light",
                    },
                )
            return resp.status_code in (200, 201)
        except Exception as e:
            log.debug("ntfy notification failed: %s", e)
            return False


# ntfy priority words -> Gotify integer priority (Gotify uses 0..10).
_GOTIFY_PRIORITY: dict[str, int] = {
    "min": 0,
    "low": 2,
    "default": 5,
    "high": 8,
    "urgent": 10,
    "max": 10,
}


class GotifyNotifier(Notifier):
    """Gotify notifier — POST ``/message?token=<app-token>`` with a JSON body.

    Gotify is the closest analogue to ntfy (a self-hosted push server with a
    plain HTTP API), which makes it the natural first alternate provider.
    """

    name = "gotify"

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token

    async def send(self, title: str, message: str, priority: str = "default") -> bool:
        if not self._url or not self._token:
            log.error("gotify notifier missing url/token — not sending")
            return False
        target = f"{self._url}/message"
        if not _egress_allowed(self.name, target):
            return False
        try:
            async with pinned_async_client(timeout=5) as client:
                resp = await client.post(
                    target,
                    params={"token": self._token},
                    json={
                        "title": title,
                        "message": message,
                        "priority": _GOTIFY_PRIORITY.get(priority, _GOTIFY_PRIORITY["default"]),
                    },
                )
            return resp.status_code in (200, 201)
        except Exception as e:
            # Log the exception *type* only — a stringified httpx error can echo
            # the request URL, which here carries the secret ?token= query param.
            log.debug("gotify notification failed: %s", type(e).__name__)
            return False


def _build_ntfy(cfg: Mapping[str, str], ntfy_url: str | None, ntfy_topic: str | None) -> Notifier:
    # Prefer the explicit call-site url/topic (the historical threaded params),
    # then settings, then module defaults — preserving current behavior exactly.
    url = ntfy_url or cfg.get("ntfy_url") or DEFAULT_NTFY_URL
    topic = ntfy_topic or cfg.get("ntfy_topic") or DEFAULT_NTFY_TOPIC
    return NtfyNotifier(url=url, topic=topic)


def _build_gotify(cfg: Mapping[str, str], ntfy_url: str | None, ntfy_topic: str | None) -> Notifier:
    return GotifyNotifier(url=cfg.get("gotify_url", ""), token=cfg.get("gotify_token", ""))


# Fail-closed registry. Adding a provider = one row here + a Notifier subclass.
NOTIFIER_BUILDERS: dict[str, Callable[[Mapping[str, str], str | None, str | None], Notifier]] = {
    "ntfy": _build_ntfy,
    "gotify": _build_gotify,
}


def _validated_http_url(name: str, raw: str) -> str:
    """Strip + validate an http(s) URL (empty clears it). Raises ValueError on bad scheme."""
    val = raw.strip()
    if val and not val.startswith(("http://", "https://")):
        raise ValueError(f"{name} must start with http:// or https://, got: '{val[:50]}'")
    return val


def apply_settings(
    db: Any,
    *,
    ntfy_url: str | None = None,
    provider: str | None = None,
    gotify_url: str | None = None,
    gotify_token: str | None = None,
) -> None:
    """Validate + persist notifier settings (only non-None fields). Raises ValueError on bad input.

    Provider is validated against the live ``NOTIFIER_BUILDERS`` registry — adding a
    provider there automatically widens the accepted set (no parallel allowlist).
    """
    if ntfy_url is not None:
        db.set_setting("ntfy_url", _validated_http_url("ntfy_url", ntfy_url))
    if provider is not None:
        p = provider.strip().lower()
        if p not in NOTIFIER_BUILDERS:
            raise ValueError(
                f"notifier_provider must be one of {sorted(NOTIFIER_BUILDERS)}; got: '{p[:50]}'"
            )
        db.set_setting(PROVIDER_SETTING_KEY, p)
    if gotify_url is not None:
        db.set_setting("gotify_url", _validated_http_url("gotify_url", gotify_url))
    if gotify_token is not None:
        db.set_setting("gotify_token", gotify_token.strip())


def _load_settings() -> Mapping[str, str]:
    """Read notifier-relevant settings; defensive.

    A missing/unavailable DB yields an empty mapping, which resolves to the ntfy
    default — so a notification path with no platform DB (e.g. a unit test that
    calls the chokepoint directly) keeps the historical ntfy behavior.
    """
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            out: dict[str, str] = {}
            for k in _SETTING_KEYS:
                v = db.get_setting(k)
                if v is not None:
                    out[k] = v
            return out
    except Exception as e:  # pragma: no cover - defensive
        log.debug("notifier settings unavailable, defaulting to ntfy: %s", e)
        return {}


def resolve_notifier(
    *,
    ntfy_url: str | None = None,
    ntfy_topic: str | None = None,
    settings: Mapping[str, str] | None = None,
) -> Notifier | None:
    """Build the active notifier from settings (fail-closed).

    Returns ``None`` when the configured provider name is UNKNOWN — the caller
    must treat that as a delivery failure (it must NOT silently fall back to
    ntfy). An UNSET provider yields the ntfy notifier (behavior-preserving
    default).
    """
    cfg = _load_settings() if settings is None else settings
    provider = (cfg.get(PROVIDER_SETTING_KEY) or DEFAULT_PROVIDER).strip().lower()
    builder = NOTIFIER_BUILDERS.get(provider)
    if builder is None:
        log.error(
            "unknown notifier_provider %r — refusing to send (configure one of %s)",
            provider,
            sorted(NOTIFIER_BUILDERS),
        )
        return None
    return builder(cfg, ntfy_url, ntfy_topic)


async def dispatch(
    title: str,
    message: str,
    priority: str = "default",
    *,
    ntfy_url: str | None = None,
    ntfy_topic: str | None = None,
    settings: Mapping[str, str] | None = None,
) -> bool:
    """Resolve the active notifier and deliver one notification. Never raises."""
    notifier = resolve_notifier(ntfy_url=ntfy_url, ntfy_topic=ntfy_topic, settings=settings)
    if notifier is None:
        return False
    return await notifier.send(title, message, priority)


__all__ = [
    "DEFAULT_NTFY_TOPIC",
    "DEFAULT_NTFY_URL",
    "DEFAULT_PROVIDER",
    "NOTIFIER_BUILDERS",
    "PROVIDER_SETTING_KEY",
    "GotifyNotifier",
    "Notifier",
    "NtfyNotifier",
    "dispatch",
    "resolve_notifier",
]
