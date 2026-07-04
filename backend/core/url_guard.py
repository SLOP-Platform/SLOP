"""Outbound-URL SSRF guard — one parse-based host-allowlist helper.

This is the single seam every user-influenced outbound fetch routes through, so
that the SSRF/url-substring defenses live in ONE audited place instead of being
re-implemented (fragilely) at each call site.

It replaces substring checks of the form ``"github.com" in url`` — which a host
like ``github.com.evil.example`` or a path like ``https://evil.example/github.com``
trivially defeats — with parse-based, exact-host matching.

CodeQL classes closed: ``py/full-ssrf``, ``py/partial-ssrf``,
``py/incomplete-url-substring-sanitization``.

Two guarantees:
  1. **Exact host match.** ``urlparse(url).hostname`` is compared (case-folded)
     against an explicit allowlist set. A substring can never pass.
  2. **Private/loopback/link-local IP literals are rejected** unless the caller
     explicitly opts in (``allow_private=True`` — e.g. a localhost-only probe).
     This blocks internal-network scanning via a user-supplied URL.

Scope note (honest): host literals that are private IPs are always blocked.
A *hostname* that resolves to a private IP at DNS time (rebinding) is caught
only when the caller opts in with ``resolve_dns=True`` (#1102) — which resolves
the host and rejects an internal resolved address. That is a **layer-1** guard:
a TOCTOU window remains because the OS re-resolves at connect time, so full
closure still needs connect-time IP pinning at each ``urlopen`` site (tracked
follow-up). Callers that accept arbitrary hosts (``allowed_hosts=None``) get
scheme + private-IP-literal enforcement, plus the resolved-IP check when they
pass ``resolve_dns=True``.
"""

from __future__ import annotations

import functools
import http.client
import ipaddress
import re
import socket
import urllib.request
from collections.abc import Callable, Iterable
from urllib.parse import ParseResult, urlparse

#: RFC 6598 carrier-grade NAT shared address space — internal, not flagged by
#: ``ipaddress.is_private``. Added explicitly to the private-IP block.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

#: A dot-separated label that is purely numeric or hex (``2130706433``,
#: ``0x7f000001``, ``0177``). Used to fail closed on alternate IPv4 encodings
#: that ``ipaddress.ip_address`` refuses to parse but the OS resolver honours.
_NUMERIC_LABEL = re.compile(r"^(0[xX][0-9a-fA-F]+|[0-9]+)$")


class UrlNotAllowed(ValueError):
    """Raised when a URL fails the SSRF guard. Subclass of ValueError so existing
    ``except ValueError`` handlers at call sites keep working."""


#: Hosts permitted for GitHub manifest fetches (install-from-github). Centralized
#: here so the allowlist lives with the guard, not duplicated per call site.
GITHUB_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "gist.githubusercontent.com"})


def _is_numeric_ip_encoding(host: str) -> bool:
    """True if ``host`` looks like an alternate-encoded IPv4 literal that
    ``ipaddress.ip_address`` rejects but the OS resolver honours — e.g. decimal
    ``2130706433``, hex ``0x7f000001``, octal ``0177.0.0.1``, short ``127.1``.

    A genuine DNS hostname always ends in an alphabetic TLD, so a host whose
    every dot-label is numeric/hex is treated as an IP encoding and failed closed.
    (Canonical dotted-quad IPs are parsed by ``ipaddress`` before this is reached.)
    """
    labels = host.split(".")
    return bool(labels[-1]) and all(_NUMERIC_LABEL.match(lbl) for lbl in labels)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is in a private/loopback/link-local/reserved/CGNAT range —
    i.e. an internal address a user-supplied URL must never reach (SSRF)."""
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (ip.version == 4 and ip in _CGNAT)
    )


#: Explicit cloud-metadata IP literals that are NOT caught by ``is_link_local``.
#: AWS exposes IMDS over IPv6 at ``fd00:ec2::254`` — a ULA (``fc00::/7``), so it reads
#: as ``is_private`` (which the FLOOR deliberately does NOT block, to leave LAN configs
#: working). It must therefore be denied explicitly. (The IPv4/GCP/Azure metadata IP
#: ``169.254.169.254`` and IPv6 ``fe80::/10`` are already covered by ``is_link_local``.)
#: Two other public clouds expose IMDS off the link-local block and so also need an
#: explicit literal (#1193): Oracle Cloud (OCI) at ``192.0.0.192`` (IETF Protocol
#: Assignments, ``192.0.0.0/24``) and Alibaba Cloud at ``100.100.100.200`` (CGNAT,
#: ``100.64.0.0/10``). Specific literals only — blocking the IP, never the surrounding
#: range, so legitimate CGNAT/assignment-block configs stay unaffected.
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDS over IPv6 (ULA)
        ipaddress.ip_address("192.0.0.192"),  # Oracle Cloud (OCI) IMDS
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud IMDS
    }
)


def _is_metadata_or_link_local(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is a cloud-metadata endpoint or a link-local address.

    This is the POLICY-FREE SSRF floor for user-configured outbound URLs on a
    self-hosted platform (#1193): unlike :func:`_is_blocked_ip` (which blocks ALL
    private/loopback ranges), this denies ONLY the always-illegitimate targets — a
    self-hosted user legitimately points a notifier/registry at a private LAN address
    (``192.168.x`` / ``10.x``) or ``localhost``, but NEVER at ``169.254.169.254`` or a
    link-local address. So these can be blocked with zero false positives, independent
    of the broader private-IP-policy decision (#1193 DToC). ``169.254.169.254`` is the
    AWS/GCP/Azure IMDS IP (within ``169.254.0.0/16`` link-local).

    An IPv4-mapped IPv6 literal (``::ffff:169.254.169.254``) is normalized to its IPv4
    form FIRST — otherwise it reads as ``is_private`` (not ``is_link_local``) and would
    slip the floor straight to the metadata endpoint (SSRF bypass)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_link_local or ip in _METADATA_IPS)


def _reject_private_ip(host: str, url: str) -> None:
    """Raise if ``host`` is an IP literal in a private/loopback/link-local/etc range.

    A genuine (alphabetic) hostname is left alone here — DNS-time resolution is
    handled separately by ``_reject_private_resolved_ip`` (opt-in). But an
    ALTERNATE numeric encoding of an IP is failed closed, because ``ipaddress``
    won't canonicalize it yet the OS will resolve it (SSRF bypass).
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _is_numeric_ip_encoding(host):
            raise UrlNotAllowed(
                f"Refusing ambiguous numeric host {host!r} (alternate IP encoding): {url!r}"
            ) from None
        return  # genuine hostname, not an IP literal
    if _is_blocked_ip(ip):
        raise UrlNotAllowed(f"Refusing private/loopback/link-local address {host!r}: {url!r}")


def _reject_private_resolved_ip(host: str, url: str) -> None:
    """Resolve ``host`` via DNS and raise if ANY resolved address is internal.

    Closes the DNS-rebinding-to-private gap for open-allowlist fetches: a genuine
    hostname (e.g. an attacker-supplied model URL) that resolves to a private /
    loopback / cloud-metadata (169.254.169.254) address is rejected before the
    fetch. **Layer-1 defense** — there is still a TOCTOU window because the OS
    re-resolves at connect time; full closure needs connect-time IP pinning (a
    separate, larger change at each ``urlopen`` site).

    Behavior:
      * Host that is an IP literal → skip (already covered by ``_reject_private_ip``).
      * Resolution FAILURE → fail-open (an unresolvable host cannot be fetched
        anyway, so there is nothing to protect; and it keeps offline/hermetic
        callers from spuriously failing). Only a SUCCESSFUL resolution to an
        internal address is rejected.
    """
    try:
        ipaddress.ip_address(host)
        return  # IP literal — _reject_private_ip already handled it
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return  # unresolvable → unfetchable → nothing to block (fail-open)
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise UrlNotAllowed(f"Host {host!r} resolves to internal address {addr}: {url!r}")


def assert_allowed_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    schemes: tuple[str, ...] = ("https",),
    allow_private: bool = False,
    resolve_dns: bool = False,
) -> ParseResult:
    """Validate ``url`` for outbound fetch; return the parsed result or raise.

    Args:
        url: the URL about to be fetched.
        allowed_hosts: exact hostnames permitted (case-insensitive). ``None``
            means any host is allowed *as far as the allowlist is concerned* —
            scheme and private-IP checks still apply.
        schemes: permitted URL schemes (default https-only).
        allow_private: if True, skip the private/loopback/link-local IP rejection
            (for deliberately localhost-targeted probes). Implies no DNS resolve.
        resolve_dns: if True (and not ``allow_private``), ALSO resolve the host
            and reject when it resolves to an internal address — the
            DNS-rebinding-to-private guard for open-allowlist fetches (#1102).
            Opt-in because it performs a DNS lookup; off by default so pure
            string-validation callers are unchanged.

    Returns:
        The ``urllib.parse.ParseResult`` (so callers can reuse validated parts).

    Raises:
        UrlNotAllowed: on any scheme / host / private-IP violation.
    """
    parsed = urlparse(url)

    scheme = (parsed.scheme or "").lower()
    if scheme not in schemes:
        raise UrlNotAllowed(
            f"URL scheme {scheme!r} not permitted (allowed: {sorted(schemes)}): {url!r}"
        )

    host = (parsed.hostname or "").lower()
    if host.endswith("."):
        host = host[:-1]  # a single trailing dot is a valid-but-equivalent FQDN
    if not host:
        raise UrlNotAllowed(f"URL has no host: {url!r}")

    if allowed_hosts is not None:
        allowed = {h.lower() for h in allowed_hosts}
        if host not in allowed:
            raise UrlNotAllowed(f"Host {host!r} not in allowlist {sorted(allowed)}: {url!r}")

    if not allow_private:
        _reject_private_ip(host, url)
        if resolve_dns:
            _reject_private_resolved_ip(host, url)

    return parsed


def is_allowed_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    schemes: tuple[str, ...] = ("https",),
    allow_private: bool = False,
    resolve_dns: bool = False,
) -> bool:
    """Boolean form of :func:`assert_allowed_url` — True iff the URL passes."""
    try:
        assert_allowed_url(
            url,
            allowed_hosts=allowed_hosts,
            schemes=schemes,
            allow_private=allow_private,
            resolve_dns=resolve_dns,
        )
        return True
    except UrlNotAllowed:
        return False


def assert_not_metadata_url(url: str, *, resolve_dns: bool = True) -> None:
    """SSRF FLOOR for user-configured outbound URLs fetched over httpx (#1193).

    Rejects ONLY cloud-metadata + link-local targets — the policy-free always-deny
    set (see :func:`_is_metadata_or_link_local`). It deliberately does NOT reject a
    general private/LAN address: on a self-hosted platform a user legitimately points
    a notifier/registry/LLM endpoint at ``192.168.x`` / ``10.x`` / ``localhost``, so a
    blanket private-IP reject (the urllib seam's ``allow_private=False`` default) would
    break valid configs. The broader private-IP policy stays a DToC/operator decision
    (#1193); this floor is the part that needs no decision.

    Checks the host as an IP literal, and — when ``resolve_dns`` (default) — the
    DNS-resolved addresses too, so a hostname that resolves to ``169.254.169.254``
    (DNS-rebind to metadata) is also caught. This is a **layer-1** guard: a TOCTOU
    window remains because httpx re-resolves at connect time (full closure needs a
    connect-time-pinning httpx transport — the larger seam tracked in #1193). Unlike
    :func:`assert_allowed_url`, this does NOT constrain scheme or allowlist — it is a
    pure additive deny-floor a call site applies just before its own fetch.

    Fail-open on an empty / unresolvable / unparseable host (nothing to fetch ⇒
    nothing to protect; keeps offline/hermetic callers from spuriously failing).

    Raises:
        UrlNotAllowed: if the host is, or resolves to, a metadata/link-local address.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # An ALTERNATE numeric encoding (decimal 2852039166, hex 0xA9FEA9FE, octal
        # 0251.0376.0251.0376) that ``ipaddress`` refuses but the OS resolver honours →
        # fail CLOSED, exactly as ``_reject_private_ip`` does. Otherwise a decimal-encoded
        # 169.254.169.254 slips straight to IMDS even with resolve_dns off (a genuine
        # SSRF bypass — caught by k-2so's #1193 site-3 review). A genuine alphabetic
        # hostname is NOT numeric → falls through to the optional resolve leg below.
        if _is_numeric_ip_encoding(host):
            raise UrlNotAllowed(
                f"Refusing ambiguous numeric host {host!r} (alternate IP encoding): {url!r}"
            ) from None
    else:
        if _is_metadata_or_link_local(ip):
            raise UrlNotAllowed(f"Refusing cloud-metadata/link-local address {host!r}: {url!r}")
        return  # public/private literal — no DNS needed
    if not resolve_dns:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return  # unresolvable → unfetchable → nothing to block (fail-open)
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_metadata_or_link_local(ip):
            raise UrlNotAllowed(
                f"Host {host!r} resolves to cloud-metadata/link-local address {info[4][0]}: {url!r}"
            )


# ── Connect-time IP pinning (closes the resolve→connect TOCTOU, #1150) ──────────
# ``assert_allowed_url(resolve_dns=True)`` is a layer-1 guard: it resolves the host
# and rejects an internal address, but the OS RE-RESOLVES at connect time, so a
# DNS-rebinding attacker can answer with a public IP at validation and a private IP
# (127.0.0.1 / 169.254.169.254 / 10.0.0.0/8 …) at connect. The opener below closes
# that window by resolving + validating ONCE, then connecting to the PINNED IP while
# keeping TLS SNI + cert validation bound to the original hostname. Because the
# validating handler re-runs on EVERY open, each redirect hop is re-validated and
# re-pinned too — so a 30x redirect to an internal host is also blocked.


def resolve_pinned_ip(
    host: str,
    url: str,
    *,
    block: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] = _is_blocked_ip,
) -> str:
    """Resolve ``host`` and return ONE address to pin the connection to.

    Rejects (``UrlNotAllowed``) if ANY resolved address satisfies ``block`` —
    same posture as :func:`_reject_private_resolved_ip`, but it also RETURNS the
    address so the caller can connect to exactly the IP that was validated (no
    second resolution). Raises ``OSError`` if the host does not resolve.

    ``block`` is the deny predicate, so this one resolver serves both pinning
    policies (the reuse seam for #1193):
      * urllib ``pinned_urlopen`` — default ``_is_blocked_ip`` rejects ALL
        private/loopback/internal addresses (arbitrary-host downloads).
      * httpx metadata-floor (``backend/core/url_guard_httpx``) — passes
        ``_is_metadata_or_link_local`` so a self-hosted LAN/loopback target is
        ALLOWED and pinned, while cloud-metadata/link-local stays denied.

    An IP literal is validated against ``block`` too (so a metadata literal can
    never be pinned) and returned unchanged. NB this does not parse alternate
    numeric encodings (decimal/hex/octal) — callers that accept user hostnames
    must pre-screen with :func:`assert_not_metadata_url` (``resolve_dns=False``),
    which fails those closed before this is reached.
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if block(literal):
            raise UrlNotAllowed(f"Refusing internal address literal {host!r}: {url!r}")
        return host
    pinned: str | None = None
    for info in socket.getaddrinfo(host, None):  # OSError propagates (unresolvable)
        addr = str(info[4][0])
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if block(ip):
            raise UrlNotAllowed(f"Host {host!r} resolves to internal address {addr}: {url!r}")
        if pinned is None:
            pinned = addr
    if pinned is None:
        raise UrlNotAllowed(f"Host {host!r} did not resolve to a usable address: {url!r}")
    return pinned


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that dials a PRE-VALIDATED pinned IP while keeping the TLS
    handshake (SNI + certificate validation) bound to the original hostname."""

    def __init__(self, host: str, *, _pinned_ip: str, **kw: object) -> None:
        super().__init__(host, **kw)  # type: ignore[arg-type]
        self._pinned_ip = _pinned_ip

    def connect(self) -> None:
        # Dial the PINNED IP (not self.host, which the OS would re-resolve = the TOCTOU).
        # No proxy/tunnel branch: pinned_urlopen builds an opener with NO ProxyHandler.
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # server_hostname = the REAL host (self.host), never the pinned IP — so SNI
        # and cert-hostname verification still validate against the hostname.
        self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]  # CPython HTTPSConnection internal
            sock, server_hostname=self.host
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS opener that validates the scheme/host and pins the resolved public IP
    on EVERY open — so the initial request AND every redirect hop are re-checked."""

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        # Re-validate scheme + private-IP-literal for this hop (redirects included).
        assert_allowed_url(req.full_url, allowed_hosts=None, resolve_dns=False)
        host = (urlparse(req.full_url).hostname or "").lower().rstrip(".")
        pinned = resolve_pinned_ip(host, req.full_url)
        factory = functools.partial(_PinnedHTTPSConnection, _pinned_ip=pinned)
        # Pass only the context. HTTPSHandler stores `self._context` (built by
        # create_default_context → check_hostname=True + CERT_REQUIRED) but does
        # NOT store a `self._check_hostname` attribute — referencing it raised
        # AttributeError on the happy path (every public-IP download crashed; #1150
        # follow-up). The context already carries hostname/cert verification, which
        # _PinnedHTTPSConnection.connect() applies via wrap_socket(server_hostname=host).
        return self.do_open(
            factory,
            req,
            context=self._context,  # type: ignore[attr-defined]  # HTTPSHandler internal
        )


def pinned_urlopen(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 30,
) -> http.client.HTTPResponse:
    """SSRF-hardened ``urlopen`` for user-influenced fetches over arbitrary hosts.

    https-only; private/loopback/link-local addresses rejected; the resolved public
    IP is PINNED through connect and re-validated on every redirect hop (closes the
    DNS-rebinding TOCTOU, #1150). A plain http:// (redirect) target cannot smuggle the
    connection past the pin — the opener carries no ``HTTPHandler``.

    ``method`` (e.g. ``"HEAD"``) is passed through to the ``Request``; ``None`` keeps
    urllib's default (GET), so existing GET callers are unchanged. A HEAD probe of a
    user-supplied URL (gguf preflight) thus rides the same pin as the GET download.

    Forward-proxy fallback: IP pinning is impossible THROUGH a proxy (the proxy, not
    the client, resolves+connects to the target), so when a proxy applies to ``url``
    (env ``HTTPS_PROXY``/``HTTP_PROXY`` honoured via ``getproxies``/``proxy_bypass``)
    this falls back to the proxy-honouring ``urlopen`` with the layer-1 resolve-and-
    reject-internal guard (``resolve_dns=True``); the proxy is then the egress trust
    boundary. The common no-proxy path keeps full connect-time pin closure.
    """
    assert_allowed_url(url, allowed_hosts=None, resolve_dns=False)  # fast scheme/literal fail
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    proxies = urllib.request.getproxies()
    if (proxies.get("https") or proxies.get("http")) and not urllib.request.proxy_bypass(host):
        # Proxy egress: pinning N/A. Enforce layer-1 (resolve + reject internal) and
        # let the default proxy-honouring opener carry the request through the proxy.
        assert_allowed_url(url, allowed_hosts=None, resolve_dns=True)
        proxy_req = urllib.request.Request(url, headers=headers or {}, method=method)  # noqa: S310  # nosec B310
        return urllib.request.urlopen(proxy_req, timeout=timeout)  # type: ignore[no-any-return]  # noqa: S310  # nosec B310
    opener = urllib.request.OpenerDirector()
    for handler in (
        _PinnedHTTPSHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),  # http:// (or any non-https) hop → clean URLError
    ):
        opener.add_handler(handler)
    # https-only validated above + IP pinned/re-validated per hop by the handler.
    req = urllib.request.Request(url, headers=headers or {}, method=method)  # noqa: S310  # nosec B310
    return opener.open(req, timeout=timeout)  # type: ignore[no-any-return]
