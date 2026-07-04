"""backend/agent/scrub.py — Outbound LLM identifier redaction.

Scrubs SLOP-internal identifiers from text before it is sent to an external
(cloud) LLM provider.  All replacements are deterministic, order-stable, and
idempotent: scrub(scrub(x)) == scrub(x).

Public API
----------
scrub(text, *, profile="cloud") -> str
is_external(provider) -> bool
"""

from __future__ import annotations

import re

from backend.core.agent import _CLOUD_PROVIDERS

# ---------------------------------------------------------------------------
# Placeholder tokens — chosen to be syntactically impossible as real values
# so re-matching is safe (idempotency).
# ---------------------------------------------------------------------------
_PH_SECRET = "<SECRET>"  # noqa: S105  # placeholder constant, not a real secret
_PH_PATH = "<PATH>"
_PH_APP = "<APP>"
_PH_IP = "<IP>"
_PH_USER = "<USER>"

# ---------------------------------------------------------------------------
# Pre-compiled patterns — applied in ORDER (most-destructive first so a
# secret embedded in a path doesn't partially survive).
#
# 1. Secrets / bearer tokens   — must go first (widest risk)
# 2. Absolute SLOP paths       — before usernames so /opt/slop/…
#                                doesn't leave the literal "slop"
# 3. Container names           — slop-<app>-<n>
# 4. IPv6 literals             — before IPv4 (IPv4-mapped ::ffff: forms)
# 5. IPv4 literals
# 6. Internal usernames        — narrowest; only bare words after path/IP gone
# ---------------------------------------------------------------------------

# 1. Bearer / API-key-like tokens:
#    "Bearer <token>", "Authorization: Bearer <token>", api_key="sk-…", etc.
#    Matches typical base64url / hex / sk-style tokens of 20+ chars.
_RE_SECRET = re.compile(
    r"""
    (?:
        (?:Bearer|bearer)\s+[A-Za-z0-9\-_\.~+/]{20,}(?:={0,2})  # HTTP Bearer
      | (?:api[_\-]?key|apikey|token|secret|password|Authorization)  # labelled
        \s*[=:]\s*
        ["\']?[A-Za-z0-9\-_\.~+/!@#$%^&*]{20,}["\']?
      | \bsk-[A-Za-z0-9]{20,}                                     # OpenAI-style
      | \bghp_[A-Za-z0-9]{36,}                                    # GitHub PAT
    )
    """,
    re.VERBOSE,
)

# 2. Absolute SLOP-related paths — /opt/..., /var/..., /srv/..., /home/...,
#    plus the additional substrate roots a runtime payload can carry
#    (/data, /mnt, /tmp, /etc, /proc, /root, /usr, /run).  /var matches BROADLY
#    (var(?:/lib)?) so /var/run, /var/log, /var/tmp are all covered, not just
#    /var/lib.  Also covers standard Unix binary/library/device/boot roots:
#    /bin, /sbin, /lib, /lib64, /dev, /sys, /boot.
#    Match the full path token (up to a whitespace/quote/comma/newline).
_RE_PATH = re.compile(
    r"""
    /(?:opt|var(?:/lib)?|srv|home|data|mnt|tmp|etc|proc|root|usr|run  # original roots
      |bin|sbin|lib(?:64)?|dev|sys|boot                                # standard Unix roots
    )/\S*  # require at least one more path segment
    """,
    re.VERBOSE,
)

# 3. Docker container names: slop-<anything>-<digits>
_RE_APP = re.compile(r"\bslop-[a-zA-Z0-9_\-]+-\d+\b")

# 4. IPv6 — full form, compressed form, and IPv4-mapped (must precede IPv4)
_RE_IPV6 = re.compile(
    r"""
    (?<![:\w])          # negative lookbehind: not already part of a word/colon
    (?:
        # Full 8-group
        [0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){7}
      | # Compressed (contains ::)
        (?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}
    )
    (?![:\w])           # negative lookahead
    """,
    re.VERBOSE,
)

# 5. IPv4 — dotted-quad, optional :port
_RE_IPV4 = re.compile(
    r"""
    \b
    (?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}
    (?:25[0-5]|2[0-4]\d|[01]?\d\d?)
    (?::\d{1,5})?       # optional port
    \b
    """,
    re.VERBOSE,
)

# 6. Internal usernames — always redact bare "slop"; redact bare "stack"
#    ONLY when it carries a SLOP-identity context, either trailing
#    ("stack user/service/account/group") OR leading ("user/uid/owner/run as
#    stack").  This eliminates the "stack trace"/"call stack" over-redaction
#    (BACKLOG :151) while keeping the privacy-safe default for real username uses.
_RE_USER = re.compile(
    r"\bslop\b"
    r"|\bstack(?=\s+(?:user|service|account|group))\b"
    r"|(?<=\buser\s)stack\b"
    r"|(?<=\buid\s)stack\b"
    r"|(?<=\bowner\s)stack\b"
    r"|(?<=\bas\s)stack\b"
)

# 7. Email addresses — local@domain.tld.  Idempotency-safe: placeholders carry
#    no '@'.  Mapped to <USER> (it identifies a person/host).
_RE_EMAIL = re.compile(r"\b[\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,}\b")

# 8. Raw JWT blobs — three base64url segments, no "Bearer" prefix required.
#    A real JWT's first segment is the base64url of a JSON header and ALWAYS
#    begins with "eyJ"; anchoring on that keeps this specific (won't fire on an
#    arbitrary three-dot token like a.b.c version string) while still matching
#    short test fixtures.  Placed first in _RULES so its dotted segments aren't
#    partially clipped by another rule.  Idempotency-safe: placeholders have no
#    eyJ-prefixed two-dot base64url shape.
_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\b")

# 9. Raw high-entropy hex tokens — 32+ hex chars, word-bounded.  Catches API
#    keys / digests not carrying a labelled prefix.  Idempotency-safe (no
#    placeholder is 32 hex chars).
_RE_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{32,}\b")

# 10. Hostnames — FQDN-style or hyphenated multi-segment names that are NOT IPs
#     (IPs are matched first).  Anchored to a real TLD/internal suffix so common
#     dotted code tokens (agent.py, app.conf) and bare single-label words (nas)
#     are NOT matched — conservative to limit false-positives while still
#     catching nas-prod-01.lan / host.local / box.internal.  Mapped to <USER>.
_RE_HOSTNAME = re.compile(
    r"\b[a-zA-Z][a-zA-Z0-9\-]*(?:\.[a-zA-Z0-9\-]+)*"
    r"\.(?:local|lan|internal|home|corp|intranet|localdomain)\b"
    r"|\b[a-zA-Z][a-zA-Z0-9]*-[a-zA-Z0-9\-]*\d[a-zA-Z0-9\-]*"
    r"(?:\.[a-zA-Z0-9\-]+)+\b"
)

# Ordered list of (pattern, replacement) — idempotency relies on placeholders
# not matching any of these patterns.  ORDER matters:
#   - JWT before SECRET (its dotted shape would otherwise be clipped)
#   - HEX_TOKEN before PATH (so a hex run in a path is handled consistently)
#   - EMAIL / HOSTNAME after the IP rules (they share dotted structure; IPs win)
_RULES: list[tuple[re.Pattern[str], str]] = [
    (_RE_JWT, _PH_SECRET),  # before SECRET — JWT dots could confuse
    (_RE_SECRET, _PH_SECRET),
    (_RE_HEX_TOKEN, _PH_SECRET),
    (_RE_PATH, _PH_PATH),
    (_RE_APP, _PH_APP),
    (_RE_IPV6, _PH_IP),
    (_RE_IPV4, _PH_IP),
    (_RE_EMAIL, _PH_USER),  # after IP (emails can embed IP-like text)
    (_RE_HOSTNAME, _PH_USER),  # after IP (FQDNs that aren't IPs)
    (_RE_USER, _PH_USER),
]


def scrub(text: str, *, profile: str = "cloud") -> str:
    """Redact SLOP-internal identifiers from text bound for an external LLM.

    Redacts -> stable placeholders:
      absolute paths (/opt/slop, /var/lib/slop, /srv/...)  -> <PATH>
      container names (slop-<app>-<n>)                           -> <APP>
      IPv4 / IPv6 literals                                             -> <IP>
      internal usernames (slop, stack)                           -> <USER>
      bearer/API-key-like tokens                                       -> <SECRET>

    Pure, deterministic, idempotent. profile='local' returns text unchanged.
    None-safe: scrub(None) returns "".
    """
    if text is None:
        return ""
    if not text:
        return text
    if profile == "local":
        return text

    result = text
    for pattern, placeholder in _RULES:
        result = pattern.sub(placeholder, result)
    return result


def is_external(provider: str) -> bool:
    """True iff provider is in the cloud set (sourced from core.agent)."""
    return (provider or "").strip().lower() in _CLOUD_PROVIDERS
