"""backend/agent/classifier.py

Error classifier for the LLM agent pipeline (Phase B/C).

Public API:

  classify_offline(error_text) → ErrorClass
      Iterates DETECTION_PATTERNS in priority order.  Returns the first
      class whose any pattern matches the error text (case-insensitive).
      Falls back to ErrorClass.UNKNOWN if nothing matches.

  compute_signature_hash(error_class, error_text, app_key) → str (SHA1 hex)
      Normalises error_text (strip digits, UUIDs, hex container IDs,
      filesystem paths, ISO-8601 timestamps) then hashes the triple
      ``"<class>:<normalised>:<app_key>"``.  Stable lookup key for the
      pattern-library cache in fix_history.

  classify_with_llm(error_text, app_key, db_path) → Coroutine[tuple[ErrorClass, str, float]]
      Three-step fallback: pattern-library hit → offline classifier → LLM call.
      Gracefully degrades to (offline_class, "", 0.4) when LLM is unreachable.
      Added in Phase C.

Usage:
    from backend.agent.classifier import classify_offline, compute_signature_hash
    from backend.agent.classifier import classify_with_llm
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass

from backend.agent.taxonomy import DETECTION_PATTERNS, ErrorClass

# Legacy flat confidence the evidence-ranked scorer replaces.
# Kept as the shadow-mode fallback and the baseline logged for the shadow gate.
LEGACY_CACHE_CONFIDENCE = 0.95

# Settings flag governing enforcement of the derived score. While this is falsey
# (the default), the new score is COMPUTED and LOGGED to learning_shadow_log but
# the legacy 0.95 still drives behaviour (shadow mode). Flip to "1"/"true" to
# enforce once the shadow log proves the scorer beats the flat cache.
LEARNING_ENFORCE_SETTING = "agent_learning_enforce"

# Recency half-life and sample-size knees for the derived score.
_RECENCY_HALFLIFE_S = 2592000  # 30 days — evidence older than this is discounted
_SAMPLE_FULL_TRUST = 5  # sample size at which the size factor saturates to 1.0
_DIGEST_MISMATCH_FACTOR = 0.6  # version-blind penalty (no evidence on this digest)
_INSPECT_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Internal normalisation patterns
# ---------------------------------------------------------------------------

# ISO-8601 timestamps: 2024-01-23T12:34:56[.fractional][Z or ±hh:mm]
_RE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
# Filesystem paths (any token starting with /)
_RE_PATH = re.compile(r"/\S+")
# Long hex strings ≥8 chars (container IDs, SHAs, UUIDs without dashes)
_RE_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
# UUIDs with dashes
_RE_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
# Standalone integers / decimal numbers
_RE_DIGITS = re.compile(r"\b\d+\b")

# Pre-compiled per-class patterns for classify_offline (case-insensitive)
_COMPILED: list[tuple[ErrorClass, list[re.Pattern[str]]]] = [
    (
        error_class,
        [re.compile(pat, re.IGNORECASE) for pat in patterns],
    )
    for error_class, patterns in DETECTION_PATTERNS.items()
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_offline(error_text: str) -> ErrorClass:
    """Return the best-match ErrorClass for *error_text* using regex patterns.

    Iterates DETECTION_PATTERNS in priority order (IMAGE_PULL_FAIL first,
    UNKNOWN last).  Returns the first class whose any pattern produces a
    match.  UNKNOWN is the guaranteed fallback because its pattern list is
    empty and it appears last.

    Args:
        error_text: Raw error string from a failing install step (may be
                    multi-line).

    Returns:
        The matched ErrorClass (never None).
    """
    for error_class, compiled_patterns in _COMPILED:
        for pattern in compiled_patterns:
            if pattern.search(error_text):
                return error_class
    # Explicit fallback (also reached naturally if UNKNOWN list is empty)
    return ErrorClass.UNKNOWN


def compute_signature_hash(
    error_class: ErrorClass,
    error_text: str,
    app_key: str,
) -> str:
    """Compute a stable SHA1 hex digest for a (class, error, app) triple.

    Normalisation strips the volatile parts of *error_text* so that two
    occurrences of "the same problem" on the same app produce the same
    hash even if container IDs, line numbers, or timestamps differ.

    Normalisation order (applied sequentially):
      1. ISO-8601 timestamps
      2. Filesystem paths (token starting with /)
      3. UUIDs (with dashes)
      4. Long hex strings ≥8 chars
      5. Standalone digit sequences

    After stripping, whitespace is collapsed to single spaces and the
    result is lowercased before hashing.

    Args:
        error_class: The classified ErrorClass value.
        error_text:  Raw error string (may be multi-line).
        app_key:     Catalog key of the failing app (e.g. ``"sonarr"``).

    Returns:
        40-character lowercase SHA1 hex string.
    """
    normalised = error_text
    normalised = _RE_TIMESTAMP.sub(" ", normalised)
    normalised = _RE_PATH.sub(" ", normalised)
    normalised = _RE_UUID.sub(" ", normalised)
    normalised = _RE_HEX.sub(" ", normalised)
    normalised = _RE_DIGITS.sub(" ", normalised)
    # Collapse whitespace and lowercase
    normalised = " ".join(normalised.split()).lower()

    payload = error_class.value + ":" + normalised + ":" + app_key
    return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Phase C: LLM-enriched classifier
# ---------------------------------------------------------------------------


async def _query_llm_for_diagnosis(prompt: str) -> str | None:
    """Call the configured LLM backend with *prompt*. Returns raw text or None.

    Uses the same provider abstraction (ollama / cloud / openai-compatible) as
    the existing health checker.  All exceptions are swallowed — returns None
    if the LLM is unreachable, misconfigured, or times out.

    Deferred imports prevent circular-import issues with ``backend.core.state``
    and ``backend.health.checker``.
    """
    try:
        import json as _json
        import httpx
        from backend.core.state import StateDB
        from backend.health.checker import _load_provider_config
        from backend.agent.router.dispatch import route_and_dispatch

        provider, api_key, model, cloud_providers = _load_provider_config()

        with StateDB() as _db:
            cfg_raw = _db.get_setting("llm_agent_config")
        cfg = _json.loads(cfg_raw) if cfg_raw else {}
        if provider == "llamacpp":
            base_url = cfg.get("llamacpp_url", "http://localhost:8081")
        else:
            base_url = cfg.get("ollama_url", "http://localhost:11434")
        if not model:
            model = cfg.get("ollama_model", "phi4-mini")

        async with httpx.AsyncClient(timeout=30) as client:
            # route_and_dispatch routes every per-provider call through
            # _dispatch_llm_call (scrub preserved) and degrades to the legacy
            # single-provider path on empty chain / router error. Returns ''
            # on all-failed; preserve today's None-on-failure semantics.
            raw = await route_and_dispatch(
                client,
                prompt,
                cfg,
                ollama_url=base_url,
                model=model,
                api_key=api_key,
                cloud_providers=cloud_providers,
            )
            return raw if raw else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Evidence-ranked learning
#
# Replaces the flat-0.95 confidence cache. The score is DERIVED from recorded
# fix_history outcomes for a signature, keyed on the running image's digest so a
# fix learned on one image version is not blindly replayed on another. A later
# ``failed_verification`` demotes the score (no supersede/veto was the F2 bug).
# Runs in SHADOW MODE by default: computed + logged to learning_shadow_log, but
# the legacy 0.95 still drives behaviour until the ``agent_learning_enforce``
# flag is set (the shadow gate).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearnedConfidence:
    """Derived, evidence-ranked confidence for a cached fix recommendation.

    Fields:
      score          — derived confidence in [0, 1]. 0.0 when there is no
                       success evidence (demote-to-floor; never the flat 0.95).
      sample_size    — number of recorded outcomes behind the score.
      success_count  — successful outcomes in the window.
      failure_count  — failed_verification (or failure) outcomes — these DEMOTE.
      digest_match   — True iff at least one success shares the current image
                       digest (version-aware; absence applies a penalty).
      enforce        — True iff the ``agent_learning_enforce`` flag is set; when
                       False the caller stays in shadow mode (legacy 0.95 wins).
    """

    score: float
    sample_size: int
    success_count: int
    failure_count: int
    digest_match: bool
    enforce: bool


def _recency_factor(newest_age_s: float) -> float:
    """Exponential-decay recency weight in (0, 1] from the newest evidence age."""
    if newest_age_s <= 0:
        return 1.0
    # 0.5 ** (age / halflife): 1.0 fresh, 0.5 at one half-life, → 0 as age grows.
    return float(0.5 ** (newest_age_s / _RECENCY_HALFLIFE_S))


def derive_confidence(
    tally: dict[str, int],
    *,
    digest_match: bool,
    recency: float = 1.0,
) -> float:
    """Compute the outcome-weighted confidence from an outcome *tally*.

    score = verified_success_rate · sample_size_factor · recency · digest_factor

    - verified_success_rate = success / (success + failure)  → demote-on-failure:
      every recorded ``failed_verification`` pulls this ratio down.
    - sample_size_factor = min(1.0, total / _SAMPLE_FULL_TRUST) → low evidence
      (incl. F5 signature-collision risk) caps confidence low.
    - digest_factor = 1.0 when evidence matches the running image digest, else
      _DIGEST_MISMATCH_FACTOR (version-blind penalty).

    Returns 0.0 when there is no success evidence (never the flat 0.95).
    """
    success = int(tally.get("success", 0))
    failure = int(tally.get("failure", 0))
    decided = success + failure
    if success <= 0 or decided <= 0:
        return 0.0
    success_rate = success / decided
    sample_factor = min(1.0, decided / _SAMPLE_FULL_TRUST)
    digest_factor = 1.0 if digest_match else _DIGEST_MISMATCH_FACTOR
    score = success_rate * sample_factor * max(0.0, min(1.0, recency)) * digest_factor
    return round(max(0.0, min(1.0, score)), 4)


def current_image_digest(container: str | None) -> str:
    """Return the running container's resolved image id (``sha256:…``) or ''.

    Content-addressed handle (``docker inspect --format {{.Image}}``), the same
    source ``safe_update`` / ``image_audit`` use. Empty string on any failure so
    callers treat a missing digest as "version-blind" rather than crashing.
    """
    if not container:
        return ""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def _enforce_enabled() -> bool:
    """True iff the shadow gate is flipped to enforce the derived score."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            raw = db.get_setting(LEARNING_ENFORCE_SETTING)
    except Exception:
        return False
    return raw is not None and raw.strip().lower() in ("1", "true", "yes", "on")


def evaluate_learned_confidence(
    app_key: str,
    signature_hash: str,
    *,
    image_digest: str = "",
) -> LearnedConfidence:
    """Derive the evidence-ranked confidence for *signature_hash* and log it.

    Reads the recorded outcome tally, computes the derived score, and ALWAYS
    appends a row to ``learning_shadow_log`` (shadow gate substrate) recording
    the derived score against the legacy flat 0.95 it replaces. The returned
    ``enforce`` flag tells the caller whether to act on the derived score
    (enforce mode) or fall back to the legacy value (shadow mode).

    Never raises — learning is best-effort; on any DB failure the caller keeps
    its existing behaviour with a zero-evidence LearnedConfidence.
    """
    enforce = _enforce_enabled()
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            tally = db.learning_outcome_tally(signature_hash, image_digest=image_digest or None)
            digest_match = bool(image_digest) and tally.get("digest_match", 0) > 0
            score = derive_confidence(tally, digest_match=digest_match)
            db.record_learning_shadow(
                app_key=app_key,
                signature_hash=signature_hash,
                image_digest=image_digest,
                learned_score=score,
                legacy_score=LEGACY_CACHE_CONFIDENCE,
                sample_size=tally.get("total", 0),
                success_count=tally.get("success", 0),
                failure_count=tally.get("failure", 0),
                digest_match=digest_match,
                enforced=enforce,
            )
        return LearnedConfidence(
            score=score,
            sample_size=tally.get("total", 0),
            success_count=tally.get("success", 0),
            failure_count=tally.get("failure", 0),
            digest_match=digest_match,
            enforce=enforce,
        )
    except Exception:
        return LearnedConfidence(
            score=0.0,
            sample_size=0,
            success_count=0,
            failure_count=0,
            digest_match=False,
            enforce=enforce,
        )


async def classify_with_llm(
    error_text: str,
    app_key: str,
    db_path: str,
) -> tuple[ErrorClass, str, float]:
    """Classify *error_text* using a three-step fallback strategy.

    Returns ``(error_class, suggested_fix, confidence)`` where:
    - pattern-library exact-hash hit (LLM skipped) → confidence is the
      evidence-ranked derived score in **enforce** mode, or the legacy
      ``0.95`` in **shadow** mode (the derived score is still logged). See
      ``evaluate_learned_confidence`` and the ``agent_learning_enforce`` flag.
    - ``confidence=0.8``   — LLM responded and regex class was not UNKNOWN
    - ``confidence=0.5``   — LLM responded but class is UNKNOWN
    - ``confidence=0.4``   — LLM unreachable; offline result kept, no suggestion

    Three-step fallback:
    1. **Pattern-library hit** — query ``fix_history`` for a prior successful
       fix with the same ``signature_hash``.  If found, derive an
       evidence-ranked confidence (outcome-weighted, image_digest-aware,
       demote-on-failure) and log it to the shadow gate; the returned
       confidence is the derived score only when enforcement is enabled,
       otherwise the legacy 0.95 (shadow mode).
    2. **Offline class + LLM enrichment** — run ``classify_offline`` to get
       the error class, then call the LLM for a human-readable suggested fix.
    3. **Graceful degrade** — if the LLM is unreachable at any point, return
       ``(offline_class, "", 0.4)``.

    Args:
        error_text: Raw error string from a failing install step.
        app_key:    Catalog key of the failing app (e.g. ``"sonarr"``).
        db_path:    Path to the SQLite state database file.  Used for the
                    pattern-library ``fix_history`` lookup.

    Returns:
        ``(ErrorClass, suggested_fix_str, confidence_float)`` — never raises.
    """
    import sqlite3

    # Compute offline class and stable hash — pure, always succeeds.
    error_class = classify_offline(error_text)
    sig_hash = compute_signature_hash(error_class, error_text, app_key)

    # Step 1 — pattern-library lookup (exact hash hit on prior successful fix).
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT suggested_fix, image_digest FROM fix_history "
                "WHERE signature_hash=? AND outcome='success' "
                "ORDER BY created_at DESC LIMIT 1",
                (sig_hash,),
            ).fetchone()
            conn.close()
            if row:
                # Evidence-ranked: derive an outcome-weighted, image_digest-aware
                # score (demote-on-failure) and log it to the shadow gate. In
                # shadow mode the legacy 0.95 still drives behaviour; only the
                # enforce flag promotes the derived score to the return value.
                cached_digest = row["image_digest"] if "image_digest" in row.keys() else ""
                learned = evaluate_learned_confidence(
                    app_key, sig_hash, image_digest=cached_digest or ""
                )
                confidence = learned.score if learned.enforce else LEGACY_CACHE_CONFIDENCE
                return (error_class, row["suggested_fix"], confidence)
        except Exception:  # noqa: S110  # best-effort DB lookup; fall through to LLM if unavailable
            pass

    # Step 2 — build context and call LLM.
    # Deferred import: assemble_context may reference backend.core.state internally.
    from backend.health.context_assembler import assemble_context

    context_block = assemble_context(
        app_key,
        "install_monitor",
        runtime={"error_class": error_class.value, "error_text": error_text[:500]},
    )
    prompt = (
        "You are a Docker install troubleshooter. "
        "Diagnose the following installation failure and suggest a fix.\n\n"
        + context_block
        + "\n\nError class: "
        + error_class.value
        + "\nError: "
        + error_text[:500]
        + "\n\nReply with one short paragraph (plain text, ≤200 chars) describing the fix."
    )

    raw = await _query_llm_for_diagnosis(prompt)

    if raw is None:
        # Step 3 — graceful degrade: LLM unreachable.
        return (error_class, "", 0.4)

    # Parse: first non-empty paragraph, truncate to 200 chars.
    first_para = next(
        (p.strip() for p in raw.split("\n\n") if p.strip()),
        raw.strip(),
    )
    suggested_fix = first_para[:200]
    confidence = 0.8 if error_class != ErrorClass.UNKNOWN else 0.5
    return (error_class, suggested_fix, confidence)
