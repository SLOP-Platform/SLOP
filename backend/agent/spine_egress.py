"""backend/agent/spine_egress.py — the deny-by-default egress trust boundary.

This is the SINGLE chokepoint every outbound (LLM-review) payload passes through.
It implements the load-bearing safety property of the spine (survey §4, review
R1-R5):

  **The primary control is an ALLOWLIST, not a scrubber.**  A cloud-bound payload
  is a STRUCTURED, allowlisted projection of a :class:`Finding` — a fixed set of
  scalar keys (``id``, ``physics``, ``verdict``, ``summary``) — NEVER a free-form
  blob carrying ``detail``/logs/paths/hostnames.  ``scrub(profile="cloud")`` runs
  as defense-in-depth ON TOP of the allowlist, never as the gate.

  **Fail closed = provable cleanliness (R2).**  We send only if an INDEPENDENT
  verifier confirms the serialized payload contains only allowlisted keys whose
  string values carry no residual leak markers.  On any disallowed content OR a
  verifier failure we return a recorded INDETERMINATE and DO NOT send.  We never
  rely on "did scrub raise?" — scrub is pure regex on str and cannot raise; the
  real risk is silent under-redaction, which the allowlist forecloses.

  **Provider deny-by-default (R3/R4).**  The egress decision keys off the
  per-attempt provider identity.  An unknown/unclassified provider (not in the
  local set) is treated as CLOUD (allowlist+scrub), never silently local.  The
  classification is sourced LIVE from ``backend.core.agent`` so it cannot drift
  from a stale copy (BACKLOG :130).

  **No copy-leak (R5).**  This module logs ONLY provider name + redaction/allowlist
  counts — NEVER raw or scrubbed payload content — and never puts a payload into a
  log argument or an exception message.

The result is an :class:`EgressOutcome`: either ``sent=True`` with the cleaned
allowlisted payload that an interpreter (S3) hands to ``route_and_dispatch``, or
``sent=False`` with a recorded ``INDETERMINATE`` reason.
"""

from __future__ import annotations

import hashlib
import re as _re
from dataclasses import dataclass
from typing import Any

from backend.agent.scrub import scrub
from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# The allowlist — the gate.  ONLY these keys ever leave the boundary.
# ---------------------------------------------------------------------------

# Scalar Finding keys permitted in an outbound payload.  Note the ABSENCE of
# ``detail`` (free-form, may carry logs/paths) and ``annotations`` — they are
# never serialized outbound.
ALLOWED_KEYS: frozenset[str] = frozenset({"id", "physics", "verdict", "summary"})

# The verdict values are a fixed, closed vocabulary — an allowlist in itself.
ALLOWED_VERDICTS: frozenset[str] = frozenset(v.value for v in Verdict)

# Leak markers that MUST NOT survive into a string value after scrub.  These are
# the independent verifier's red flags: an unredacted absolute path, an '@' (host
# or email), or a long digit/hex run.  scrub() should have neutralized real cases;
# any survivor means the payload is NOT provably clean -> fail closed.
#
# IMPORTANT: this deny-root list is INDEPENDENT of _RE_PATH in scrub.py — it must
# be a strict SUPERSET of the scrubber's roots so that a scrubber gap cannot
# silently become a verifier gap.  When adding roots to scrub.py also add them here.
# Scrubber roots: opt, var, srv, home, data, mnt, tmp, etc, proc, root, usr, run,
#                 bin, sbin, lib, lib64, dev, sys, boot

_LEAK_PATTERNS: tuple[_re.Pattern[str], ...] = (
    _re.compile(  # abs path roots
        r"/(?:opt|var|srv|home|data|mnt|tmp|etc|proc|root|usr|run"
        r"|bin|sbin|lib(?:64)?|dev|sys|boot)\b"
    ),
    _re.compile(r"@"),  # email / user@host
    _re.compile(r"\d{1,3}(?:\.\d{1,3}){3}"),  # dotted-quad IP (no \b — catches IP-in-word)
    _re.compile(r"[0-9a-fA-F]{16,}"),  # long hex / token run (no \b)
    _re.compile(r"\b[Bb]earer\b"),  # residual 'Bearer ' after token scrub
    _re.compile(
        r"\b[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}"
    ),  # JWT-shaped (3 dot-segments)
    # Hostname-shaped: a label CONTAINING A DIGIT and a hyphen (nas-prod-01), or a
    # dotted FQDN whose TLD-label looks like a real domain.  Requiring ≥4 alpha chars
    # before the hyphen avoids false-positives on SLOP-internal tokens (spine-*, ms-*)
    # while keeping real hostname shapes (server-01, nas-prod-01) blocked.
    # Gate B (_HOSTNAME_PATTERN_IDX + _P7_ALLOWLIST_PREFIXES) skips this pattern for
    # known-safe short-prefix tokens (batch-*, slop-*) that still pass Gate A.
    _re.compile(
        r"\b[a-zA-Z][a-zA-Z0-9]{3,}-[a-zA-Z0-9\-]*\d[a-zA-Z0-9\-]*\b"
    ),  # hyphenated host w/ digit
    _re.compile(
        r"\b[a-zA-Z0-9][a-zA-Z0-9\-]*\.(?:local|lan|com|net|org|io|internal)\b"
    ),  # FQDN w/ real TLD
)

# Index of the hostname pattern (Pattern 7, 0-based) in _LEAK_PATTERNS.
# Gate B: full hyphenated tokens that START with a known SLOP-native prefix are
# exempt from Pattern 7 only — all other patterns still apply.  We extract the full
# hyphenated word (not just the regex sub-match) so that a token like "ms-health-1"
# is evaluated as a whole starting with "ms-", not as the sub-match "health-1".
# This is an exhaustive closed allowlist; any novel prefix is conservatively blocked.
_HOSTNAME_PATTERN_IDX: int = 6
_P7_ALLOWLIST_PREFIXES: frozenset[str] = frozenset({"batch-", "slop-", "ms-"})
# Extracts full hyphenated alphanum tokens (word + optional hyphen-suffixes).
_HYPHEN_TOKEN_RE: _re.Pattern[str] = _re.compile(
    r"\b[a-zA-Z0-9][a-zA-Z0-9\-]*[a-zA-Z0-9]\b|\b[a-zA-Z0-9]\b"
)

# A controlled finding id: dotted snake_case segments only (e.g. self_audit.db_record).
_ID_SHAPE = _re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")


@dataclass
class EgressOutcome:
    """Result of one egress attempt — NEVER carries raw payload content in a way
    a caller could leak; ``payload`` holds only the cleaned allowlisted dict."""

    sent: bool
    provider: str
    payload: dict[str, Any] | None = None
    verdict: Verdict = Verdict.VERIFIED
    reason: str = ""
    # Counts only — defense-in-depth telemetry, never content.
    redaction_count: int = 0
    fields_sent: int = 0


# ---------------------------------------------------------------------------
# Provider classification — sourced LIVE (deny-by-default)
# ---------------------------------------------------------------------------


def _local_providers() -> frozenset[str]:
    """Live local-provider set (ollama + on-host OpenAI-compatible)."""
    try:
        from backend.core.agent import _LOCAL_OAI_PROVIDERS

        return frozenset({"ollama"}) | _LOCAL_OAI_PROVIDERS
    except Exception:  # unknown set -> nothing is local (deny)
        return frozenset()


def is_cloud_bound(provider: str) -> bool:
    """Deny-by-default: a provider is cloud-bound unless it is KNOWN-local.

    An unknown/unclassified provider is treated as cloud (allowlist+scrub), never
    silently local.  Classification is read live so it cannot drift.
    """
    name = (provider or "").strip().lower()
    if not name:
        return True  # empty/unknown provider -> deny-by-default cloud
    return name not in _local_providers()


# ---------------------------------------------------------------------------
# Allowlist projection + independent verifier
# ---------------------------------------------------------------------------


def _project_allowlisted(finding: Finding, *, cloud: bool) -> tuple[dict[str, Any], int]:
    """Project a Finding to its allowlisted scalar shape.

    For cloud routes the string fields are scrubbed (defense-in-depth).  Returns
    the payload dict AND the redaction count (how many chars scrub changed — a
    count only, never content).
    """
    raw_summary = finding.summary or ""
    raw_physics = finding.physics or ""
    if cloud:
        summary = scrub(raw_summary, profile="cloud")
        physics = scrub(raw_physics, profile="cloud")
    else:
        summary, physics = raw_summary, raw_physics
    redactions = (summary.count("<") - raw_summary.count("<")) + (
        physics.count("<") - raw_physics.count("<")
    )
    payload = {
        "id": str(finding.id),
        "physics": physics,
        "verdict": finding.verdict.value,
        "summary": summary,
    }
    return payload, max(redactions, 0)


def _scan_for_leak_markers(text: str) -> bool:
    """Return True if *text* contains any residual leak marker.

    Applies every pattern in :data:`_LEAK_PATTERNS`.  Pattern 7 (hostname-shaped)
    uses Gate A+B: each FULL hyphenated token is tested individually, and tokens
    starting with a known SLOP-native prefix are exempt.  This is the single
    canonical leak-scan used by both :func:`verify_clean` and
    :func:`verify_contribute_clean`.
    """
    for idx, pat in enumerate(_LEAK_PATTERNS):
        if idx == _HOSTNAME_PATTERN_IDX:
            # Gate A+B: extract each FULL hyphenated token from the value and
            # test it individually.  Using the full token (not a sub-match)
            # ensures "ms-health-1" is evaluated as a whole (starts with "ms-")
            # rather than letting the regex engine find the sub-match "health-1".
            for tok_m in _HYPHEN_TOKEN_RE.finditer(text):
                tok = tok_m.group(0)
                if not pat.search(tok):
                    continue  # Gate A: token doesn't look like a hostname
                tok_lower = tok.lower()
                if any(tok_lower.startswith(pfx) for pfx in _P7_ALLOWLIST_PREFIXES):
                    continue  # Gate B: token is a known-safe SLOP-native prefix
                return True
        elif pat.search(text):
            return True
    return False


def verify_clean(payload: dict[str, Any]) -> tuple[bool, str]:
    """Independent verifier — the fail-closed gate.

    Confirms the payload contains ONLY allowlisted keys, a valid verdict from the
    closed vocabulary, and no residual leak marker in any string value.  Returns
    (clean, reason).  A False here means DO NOT SEND.  Returns a reason that
    NEVER echoes payload content (R5) — only the offending KEY/category.
    """
    extra = set(payload.keys()) - ALLOWED_KEYS
    if extra:
        return False, f"disallowed key(s): {sorted(extra)}"
    if payload.get("verdict") not in ALLOWED_VERDICTS:
        return False, "verdict not in closed vocabulary"
    # ``id`` is a CONTROLLED identifier (reconciler-assigned): dotted snake_case,
    # no spaces/paths/leak vectors.  Validate its SHAPE rather than leak-scan it
    # (a dotted id like 'self_audit.integrity' is legitimate, not a leak).
    fid = payload.get("id", "")
    if not isinstance(fid, str) or not _ID_SHAPE.fullmatch(fid):
        return False, "id is not a controlled identifier shape"
    # ``summary`` and ``physics`` are the free-form-ish fields — leak-scan them.
    for key in ("summary", "physics"):
        value = payload.get(key, "")
        if not isinstance(value, str):
            return False, f"non-string value for {key!r}"
        if _scan_for_leak_markers(value):
            return False, f"residual leak marker in {key!r}"
    return True, "clean"


# ---------------------------------------------------------------------------
# The egress seam — the single function every outbound payload passes through
# ---------------------------------------------------------------------------


def send_for_review(finding: Finding, *, provider: str) -> EgressOutcome:
    """Prepare a Finding for outbound LLM review through the deny-by-default gate.

    Returns an :class:`EgressOutcome`.  On a provably-clean allowlisted payload:
    ``sent=True`` with ``payload`` (the dict the interpreter forwards to the
    router).  On any disallowed content or verifier failure: ``sent=False`` with a
    recorded INDETERMINATE — NO outbound payload is produced.  Never raises; never
    logs payload content.

    This function does NOT itself call the network — it produces the provably-clean
    payload (or refuses).  S3 (``spine_review.py``) hands ``outcome.payload`` to
    ``route_and_dispatch``; keeping the network call out of the boundary keeps the
    boundary pure and testable.
    """
    cloud = is_cloud_bound(provider)
    try:
        payload, redactions = _project_allowlisted(finding, cloud=cloud)
        clean, reason = verify_clean(payload)
    except Exception as exc:  # verifier failure => fail closed
        log.warning(
            "egress verifier failed; refusing send", provider=provider, error=type(exc).__name__
        )
        return EgressOutcome(
            sent=False,
            provider=provider,
            verdict=Verdict.INDETERMINATE,
            reason="verifier error — fail closed",
        )

    if not clean:
        # Fail closed.  Log the CATEGORY only, never content.
        log.warning("egress refused: payload not provably clean", provider=provider, reason=reason)
        return EgressOutcome(
            sent=False,
            provider=provider,
            verdict=Verdict.INDETERMINATE,
            reason=f"not provably clean: {reason}",
        )

    log.info(
        "egress allowed", provider=provider, cloud=cloud, redactions=redactions, fields=len(payload)
    )
    return EgressOutcome(
        sent=True,
        provider=provider,
        payload=payload,
        verdict=finding.verdict,
        reason="clean",
        redaction_count=redactions,
        fields_sent=len(payload),
    )


# ---------------------------------------------------------------------------
# 2c Contribute-back channel — separate allowlist, same fail-closed pattern
# ---------------------------------------------------------------------------

CONTRIBUTE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "error_class",
        "app_key",
        "suggested_fix",
        "diagnosis_class",
        "fix_type",
        "confidence",
        "sample_size",
    }
)

_CONTRIBUTE_STRING_KEYS: frozenset[str] = frozenset(
    {"error_class", "app_key", "suggested_fix", "diagnosis_class", "fix_type"}
)


def verify_contribute_clean(payload: dict[str, Any]) -> tuple[bool, str]:
    """Independent verifier for the 2c contribute-back channel.

    Confirms the payload contains ONLY allowlisted keys, that string fields are
    actually strings, and that no residual leak marker survives in suggested_fix
    (the one free-text field).  Returns (clean, reason) — a False here means
    DO NOT CONTRIBUTE.  Reason NEVER echoes payload content (R5).
    """
    extra = set(payload.keys()) - CONTRIBUTE_ALLOWED_KEYS
    if extra:
        return False, f"disallowed key(s): {sorted(extra)}"
    if not payload:
        return False, "empty payload"
    # string fields that ARE present must actually be strings
    for key in _CONTRIBUTE_STRING_KEYS:
        if key in payload and not isinstance(payload[key], str):
            return False, f"non-string value for {key!r}"
    # confidence and sample_size, when present, must be numeric
    for key in ("confidence", "sample_size"):
        if key in payload and not isinstance(payload[key], (int, float)):
            return False, f"non-numeric value for {key!r}"
    # suggested_fix is the only free-text field — leak-scan it
    fix_text = payload.get("suggested_fix", "")
    if _scan_for_leak_markers(fix_text):
        return False, "residual leak marker in contribute payload"
    return True, "clean"


def derive_contribute_key(
    error_class: str,
    app_key: str,
    diagnosis_class: str,
    fix_type: str,
) -> str:
    """Re-derive the contribution key from 4 closed-vocabulary allowlisted inputs.

    Security review §2.1: the original ``signature_hash`` is allowlist-UNSAFE
    (hostnames, tokens survive SHA256).  This key is derived from scratch using
    ONLY allowlisted, closed-vocabulary fields — zero free text.  Identical
    inputs produce identical keys across installs (unlinkable, ADR 0011).

    Returns a 40-char hex SHA1 digest.
    """
    seed = f"{error_class}:{app_key}:{diagnosis_class}:{fix_type}"
    # usedforsecurity=False: this is a deduplication key, not a password hash
    # (security review §2.1).
    return hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()


def send_contribute_back(payload: dict[str, Any]) -> EgressOutcome:
    """Prepare a 2c contribute-back payload through the fail-closed allowlist gate.

    On a provably-clean allowlisted payload: ``sent=True`` with the scrubbed
    payload.  On any disallowed content or verifier failure: ``sent=False``
    with a recorded INDETERMINATE.  Never raises; never logs payload content.

    Does NOT itself transmit to the moderation repo — returns the outcome so
    the network call stays outside the boundary (same pattern as send_for_review).
    """

    try:
        clean, reason = verify_contribute_clean(payload)
    except Exception as exc:
        log.warning(
            "contribute verifier failed; refusing send",
            error=type(exc).__name__,
        )
        return EgressOutcome(
            sent=False,
            provider="contribute-back",
            verdict=Verdict.INDETERMINATE,
            reason="verifier error — fail closed",
        )

    if not clean:
        log.warning("contribute refused: payload not provably clean", reason=reason)
        return EgressOutcome(
            sent=False,
            provider="contribute-back",
            verdict=Verdict.INDETERMINATE,
            reason=f"not provably clean: {reason}",
        )

    # Scrub the one free-text field (defense-in-depth, same pattern as send_for_review)
    redactions = 0
    raw_fix = payload.get("suggested_fix", "")
    if raw_fix:
        scrubbed = scrub(raw_fix, profile="cloud")
        redactions = max(scrubbed.count("<") - raw_fix.count("<"), 0)
        payload["suggested_fix"] = scrubbed

    log.info(
        "contribute allowed",
        fields=len(payload),
        redactions=redactions,
    )
    return EgressOutcome(
        sent=True,
        provider="contribute-back",
        payload=dict(payload),
        verdict=Verdict.VERIFIED,
        reason="clean",
        redaction_count=redactions,
        fields_sent=len(payload),
    )


__all__ = [
    "ALLOWED_KEYS",
    "ALLOWED_VERDICTS",
    "CONTRIBUTE_ALLOWED_KEYS",
    "EgressOutcome",
    "derive_contribute_key",
    "is_cloud_bound",
    "send_contribute_back",
    "send_for_review",
    "verify_clean",
    "verify_contribute_clean",
]
