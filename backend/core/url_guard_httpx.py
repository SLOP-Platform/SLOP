"""Connect-time IP-pinned httpx clients — the layer-2 SSRF closure for #1193.

The metadata/link-local deny FLOOR (:func:`backend.core.url_guard.assert_not_metadata_url`)
is **layer-1**: it resolves the host and rejects a cloud-metadata target, but httpx
RE-RESOLVES at connect time, so a DNS-rebinding host that answers a public address at
validation and ``169.254.169.254`` at connect still slips through (a TOCTOU window).
This module closes that window for httpx exactly as ``pinned_urlopen`` does for urllib
(#1150): resolve + validate ONCE, then connect to the PINNED address while keeping TLS
SNI + certificate validation — and the HTTP ``Host`` header — bound to the original
hostname.

**Policy is the metadata FLOOR, not urllib's reject-all-private posture.** SLOP is a
self-hosted platform: a user legitimately points a notifier / registry / LLM endpoint at
a private LAN address (``192.168.x`` / ``10.x``), ``localhost``, or a docker-internal name
(``http://ollama:11434``). So those resolve, are ALLOWED, and are pinned to the validated
IP; only cloud-metadata + link-local are denied (see
:func:`backend.core.url_guard._is_metadata_or_link_local`). The broader private-IP-deny
policy stays a separate DToC decision (#1193) — this is the policy-free closure.

Pinning mechanics (httpcore 1.0.9): the transport rewrites the request URL host to the
validated IP — so httpcore's ``connect_tcp`` dials exactly that address, with no second
resolution — and sets the ``sni_hostname`` request extension to the original host, which
httpcore feeds to ``server_hostname`` at the TLS handshake. So SNI + cert hostname
verification still validate against the real hostname, never the IP. The ``Host`` header
(already the original authority, set at httpx request-build time) is preserved by the
rewrite (only the URL host changes), so server-side virtual-host routing is unaffected.

Redirects: these clients keep httpx's default ``follow_redirects=False`` — a 30x response
is returned, never followed, so a redirect can never pivot the connection to an internal
host. (If a caller opts into following redirects, each hop re-enters the transport and is
re-pinned; a *relative* redirect would resolve against the rewritten IP URL and lose its
SNI binding, so following redirects on these clients is unsupported by design.)
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx

from backend.core.url_guard import (
    UrlNotAllowed,
    _is_metadata_or_link_local,
    assert_not_metadata_url,
    resolve_pinned_ip,
)

#: The metadata-floor deny predicate — allows private LAN / loopback / docker-internal,
#: denies cloud-metadata + link-local. Bound once so both transports share one policy.
_FLOOR = _is_metadata_or_link_local


def _pin_request(request: httpx.Request) -> None:
    """Validate + pin ``request`` IN PLACE (host→IP, ``sni_hostname``, Host preserved).

    Fail-CLOSED: a metadata/link-local literal — including an alternate numeric encoding
    (decimal/hex/octal) that ``resolve_pinned_ip`` would not parse — is rejected by the
    layer-1 floor BEFORE any rewrite. Non-http(s) schemes and empty hosts are left alone.

    Raises:
        UrlNotAllowed: the host is, or resolves to, a metadata/link-local address.
        OSError: the host does not resolve (caller converts to ``httpx.ConnectError``).
    """
    if request.url.scheme not in ("http", "https"):
        return
    host = (request.url.host or "").lower().rstrip(".")
    if not host:
        return
    url = str(request.url)
    # Layer-1 floor first: rejects a metadata/link-local LITERAL and any alternate
    # numeric encoding. resolve_dns=False — the DNS leg is the pin below (no double
    # resolve, and the pin is what closes the connect-time TOCTOU anyway).
    assert_not_metadata_url(url, resolve_dns=False)
    pinned = resolve_pinned_ip(host, url, block=_FLOOR)
    if pinned == host:
        return  # host was an already-validated IP literal — nothing to rewrite
    request.url = request.url.copy_with(host=pinned)
    request.extensions = {**request.extensions, "sni_hostname": host}


class _PinnedTransport(httpx.HTTPTransport):
    """Sync transport that pins the validated IP before delegating to httpx.

    Exception contract: a disallowed metadata/link-local target raises
    ``UrlNotAllowed`` (propagated — call sites absorb it in their existing
    ``except`` handlers); an unresolvable host is converted to
    ``httpx.ConnectError`` for parity with httpx's own connect failure.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            _pin_request(request)
        except OSError as exc:  # unresolvable host → same failure httpx would raise
            raise httpx.ConnectError(str(exc), request=request) from exc
        return super().handle_request(request)


class _PinnedAsyncTransport(httpx.AsyncHTTPTransport):
    """Async transport that pins the validated IP before delegating to httpx.

    DNS resolution (``socket.getaddrinfo``) is blocking, so it runs off the event
    loop via ``anyio.to_thread``; the mutated request is then handed to httpx.
    Same exception contract as :class:`_PinnedTransport` (``UrlNotAllowed``
    propagates; unresolvable → ``httpx.ConnectError``).
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            await anyio.to_thread.run_sync(_pin_request, request)
        except OSError as exc:  # unresolvable host → same failure httpx would raise
            raise httpx.ConnectError(str(exc), request=request) from exc
        return await super().handle_async_request(request)


def pinned_client(**kwargs: Any) -> httpx.Client:
    """A sync ``httpx.Client`` whose connections are SSRF-pinned to the validated IP.

    Drop-in for ``httpx.Client(...)`` at user-configured-URL fetch sites — caller
    kwargs (``timeout``, ``headers``, …) pass through; the transport is fixed and any
    caller-supplied ``transport`` is ignored (the pin must not be bypassable).
    """
    kwargs.pop("transport", None)
    return httpx.Client(transport=_PinnedTransport(), **kwargs)


def pinned_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Async counterpart of :func:`pinned_client` (drop-in for ``httpx.AsyncClient``)."""
    kwargs.pop("transport", None)
    return httpx.AsyncClient(transport=_PinnedAsyncTransport(), **kwargs)


__all__ = ["UrlNotAllowed", "pinned_async_client", "pinned_client"]
