"""backend/health/checker_llm.py

LLM agent integration for health check diagnosis.

Extracted from checker.py (Core Rule 8.1 — file-size discipline).
All symbols are re-exported from checker.py for backward compatibility.

Responsibilities:
  - _llm_state  — module-level LLM degradation state
  - _LLM_ACTION_MAP — raw-to-canonical action mapping
  - Diagnosis prompt construction (DB context + RAG enrichment)
  - LLM error classification and state tracking
  - pending_fixes persistence

The LLM call transport/dispatch layer (ollama / cloud / openai-compatible +
router cloud-spend metering, incl. the ADR-0021 scrub choke) lives in the sibling
`checker_llm_dispatch.py` and is re-exported below for backward compatibility.
"""

from __future__ import annotations

import time
from typing import Any

from backend.core.url_guard_httpx import pinned_async_client
from backend.core.logging import get_logger
from backend.health.swallow_counter import record_swallow
from backend.manifests.executor import PERF_THRESHOLDS

# Transport layer (extracted) — re-exported so `checker_llm._call_*` /
# `checker_llm._dispatch_llm_call` and the checker.py re-export chain keep resolving.
from backend.health.checker_llm_dispatch import (
    _call_ollama as _call_ollama,
    _call_cloud_provider as _call_cloud_provider,
    _record_cloud_dispatch_usage as _record_cloud_dispatch_usage,
    _call_openai_compatible as _call_openai_compatible,
    _dispatch_llm_call as _dispatch_llm_call,
)

log = get_logger(__name__)

# LLM agent degradation state — module-level so it persists across checks
_llm_state: dict[str, Any] = {
    "status": "unknown",  # active | degraded | offline | disabled
    "consecutive_failures": 0,
    "consecutive_slow": 0,
    "last_checked": 0,
    "last_error": "",  # human-readable last error
    "last_error_type": "",  # connection | timeout | parse | auth | dns | unknown
    "ollama_url": "http://ollama:11434",  # Docker container hostname
    "model_tried": "",  # which model was requested
    "last_success_at": 0,  # unix timestamp of last successful call
    "configured_provider": "",  # provider name from llm_agent_config
    "configured_model": "",  # model name from llm_agent_config
    "offline_since": 0,  # unix timestamp when status transitioned to offline
}

# Map raw LLM action strings to internal action_type symbols (module-scope so
# `_extract_diagnosis` and tests can reuse it).
_LLM_ACTION_MAP: dict[str, str] = {
    "restart": "restart_container",
    "restart_container": "restart_container",
    "reload": "reload_config",
    "config_change": "reload_config",
    "pull": "pull_image",
    "update_image": "pull_image",
    "rewire": "rewire",
    "restart_service": "restart_managed_service",
    "remount": "remount_storage",
    "reprovision": "reprovision_hostname",
    "manual": "manual",
    "escalate": "escalate",
}


def _log_routing(
    app_key: str,
    task_type: str,
    model: str,
    success: bool,
    duration_ms: int | None,
    error_type: str | None,
    summary: str | None,
) -> None:
    """Write one routing decision to llm_routing_log. Never raises."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.execute(
                """INSERT INTO llm_routing_log
                   (app_key, task_type, model, success, duration_ms, error_type, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (app_key, task_type, model, int(success), duration_ms, error_type, summary),
            )
            # Prune entries older than 30 days
            db.execute("DELETE FROM llm_routing_log WHERE ts < (unixepoch() - 2592000)")
    except Exception as exc:
        log.debug("routing log write failed: %s", exc)


def _check_ram_for_llm(model: str) -> bool:
    """Pre-flight RAM check — return False when the model can't fit safely."""
    from backend.core.system_eval import quick_ram_check, LLM_MODEL_RAM_MB

    model_ram = LLM_MODEL_RAM_MB.get(model, 3000)
    ok, warn = quick_ram_check(model_ram)
    if not ok:
        log.debug("Skipping LLM diagnosis — %s", warn)
    return ok


def _load_provider_config() -> tuple[str, str, str, set[str]]:
    """Return (provider, api_key, model_cfg, cloud_provider_set) from llm_agent_config."""
    try:
        import json as _jcfg
        from backend.core.state import StateDB

        with StateDB() as _db:
            _cfg = _jcfg.loads(_db.get_setting("llm_agent_config") or "{}")
        _provider = _cfg.get("provider", "ollama")
        _api_key = _cfg.get("api_key", "")
        _model_cfg = _cfg.get("model", "")
        _llm_state["configured_provider"] = _provider
        _llm_state["configured_model"] = _model_cfg
        from backend.core.cloud_llm import PROVIDERS as _PROV_MAP

        return _provider, _api_key, _model_cfg, set(_PROV_MAP.keys())
    except Exception:
        return "ollama", "", "", set()


async def _maybe_rag_expand(
    raw: str, prompt: str, app_key: str, ollama_url: str, model: str, logs: str
) -> str:
    """If the model's confidence is low and we have logs, re-run with extra KB chunks.

    Always uses ollama for the expansion (preserves original behaviour). Returns
    the expanded raw string when the re-run succeeded, otherwise the original raw.
    """
    try:
        parsed = __import__("json").loads(raw)
        confidence = float(parsed.get("confidence", 1.0))
        if confidence >= 0.5 or not logs:
            return raw
        from backend.core.rag import query_knowledge_base as _qkb

        extra_chunks = _qkb(f"{app_key} {parsed.get('problem', '')} {logs[-200:]}", n=2)
        if not extra_chunks:
            return raw
        log.debug(
            "RAG expansion triggered for %s (confidence=%.2f) — re-running with %d extra chunks",
            app_key,
            confidence,
            len(extra_chunks),
        )
        expanded_context = "\n\n".join(extra_chunks)
        expanded_prompt = f"Additional knowledge base context:\n{expanded_context}\n\n{prompt}"
        async with pinned_async_client(timeout=50) as client2:
            resp2 = await client2.post(
                f"{ollama_url}/api/generate",
                json={"model": model, "prompt": expanded_prompt, "stream": False, "format": "json"},
            )
        if resp2.status_code == 200:
            raw2 = resp2.json().get("response", "")
            if isinstance(raw2, str) and raw2:
                return raw2
    except Exception:  # best-effort re-prompt for JSON; return raw text if follow-up fails
        record_swallow("checker_llm.rag_re_prompt")
    return raw


def _check_offline_recovery() -> None:
    """Transition offline status to unknown after ≥1 hour with no success.

    Once an LLM status becomes "offline", there is no automatic recovery path
    (no re-probe) until the process restarts. This function implements a recovery
    timer: if the status has been "offline" for ≥3600 seconds (1 hour),
    transition it back to "unknown" so the next health cycle re-probes.
    """
    if _llm_state["status"] != "offline":
        return

    if _llm_state.get("offline_since", 0) == 0:
        # Status is offline but we don't have a transition timestamp — record it now
        _llm_state["offline_since"] = int(time.monotonic())
        return

    elapsed_offline = int(time.monotonic()) - _llm_state["offline_since"]
    if elapsed_offline >= 3600:  # 1 hour
        _llm_state["status"] = "unknown"
        _llm_state["offline_since"] = 0
        log.info("LLM agent recovered from offline after %ds — re-probing", elapsed_offline)


def _track_llm_success(elapsed: float, ollama_url: str, model: str) -> None:
    """Record a successful LLM call in _llm_state (slow / active / degraded)."""
    if elapsed > PERF_THRESHOLDS["llm_inference_seconds"]:
        _llm_state["consecutive_slow"] += 1
    else:
        _llm_state["consecutive_slow"] = 0
        _llm_state["status"] = "active"
        _llm_state["offline_since"] = 0  # Clear offline recovery timer on success
    if _llm_state["consecutive_slow"] >= 3:
        _llm_state["status"] = "degraded"
        log.warning("LLM agent degraded — slow inference (%ds)", int(elapsed))
    _llm_state["consecutive_failures"] = 0
    _llm_state["last_error"] = ""
    _llm_state["last_error_type"] = ""
    _llm_state["last_success_at"] = int(time.monotonic())
    _llm_state["model_tried"] = model
    _llm_state["ollama_url"] = ollama_url


def _classify_llm_error(e: Exception, ollama_url: str, model: str) -> None:
    """Update _llm_state with the error classification + offline-state transition."""
    # Check if a prior offline period has elapsed ≥1 hour and recover to unknown
    _check_offline_recovery()

    _llm_state["consecutive_failures"] = _llm_state.get("consecutive_failures", 0) + 1
    err_str = str(e)
    _provider = _llm_state.get("configured_provider", "ollama") or "ollama"
    _pname = "Ollama" if _provider == "ollama" else _provider
    _host = ollama_url.split("//")[-1].split("/")[0].split(":")[0]
    if (
        "Connection refused" in err_str
        or "Connect call failed" in err_str
        or "ConnectionRefusedError" in err_str
    ):
        _llm_state["last_error_type"] = "connection"
        _llm_state["last_error"] = f"Cannot reach {ollama_url} — {_pname} may not be running."
    elif any(
        x in err_str
        for x in (
            "Name or service not known",
            "getaddrinfo failed",
            "nodename nor servname",
            "Name does not resolve",
            "Temporary failure in name resolution",
            "[Errno -2]",
            "[Errno 11001]",
        )
    ):
        _llm_state["last_error_type"] = "dns"
        _llm_state["last_error"] = (
            f"Cannot resolve host '{_host}' — check the {_pname} URL / DNS / container network."
        )
    elif "timed out" in err_str.lower() or "TimeoutError" in err_str:
        _llm_state["last_error_type"] = "timeout"
        _llm_state["last_error"] = (
            f"Request to {ollama_url} timed out — model may be too slow or overloaded."
        )
    elif "401" in err_str or "Unauthorized" in err_str or "authentication" in err_str.lower():
        _llm_state["last_error_type"] = "auth"
        _llm_state["last_error"] = (
            f"{_pname} rejected the request (401 Unauthorized) — check the API key / credentials."
        )
    elif "404" in err_str or ("model" in err_str.lower() and "not found" in err_str.lower()):
        _llm_state["last_error_type"] = "model"
        if _provider == "ollama":
            _llm_state["last_error"] = (
                f"Model '{model}' not found in Ollama — run: ollama pull {model}"
            )
        else:
            _llm_state["last_error"] = f"Model '{model}' not found in {_pname}."
    elif "JSONDecodeError" in err_str or "json" in err_str.lower():
        _llm_state["last_error_type"] = "parse"
        _llm_state["last_error"] = f"Model '{model}' returned invalid JSON — try a different model."
    else:
        _llm_state["last_error_type"] = "unknown"
        _llm_state["last_error"] = f"{type(e).__name__}: {err_str[:120]}"
    _llm_state["ollama_url"] = ollama_url
    _llm_state["model_tried"] = model
    if _llm_state["last_error_type"] in ("dns", "auth"):
        _llm_state["status"] = "offline"
        _llm_state["offline_since"] = int(time.monotonic())
        log.warning(
            "LLM agent offline (immediate): %s — %s",
            _llm_state["last_error_type"],
            _llm_state["last_error"],
        )
    elif _llm_state["consecutive_failures"] >= PERF_THRESHOLDS["llm_parse_fail_streak"]:
        _llm_state["status"] = "offline"
        _llm_state["offline_since"] = int(time.monotonic())
        log.warning(
            "LLM agent offline after %d consecutive failures: %s",
            _llm_state["consecutive_failures"],
            _llm_state["last_error"],
        )


def _persist_pending_fix(
    app_key: str,
    check_name: str,
    action_type: str,
    problem: str,
    suggested: str,
    confidence: float,
    model: str,
) -> None:
    """Upsert the LLM's diagnosis into the pending_fixes approval queue.

    Before persisting, checks fix_history for repeated failed_verification outcomes
    on (app_key, action_type). If >= 3 failures occurred within the last 24h, the
    action_type is overridden to 'escalate' so A3's escalation dispatcher can handle
    it rather than re-suggesting a fix that has already been tried and failed repeatedly.
    """
    # Failed-fix read-back gate: stop re-suggesting a fix that repeatedly fails
    # verification. Override to 'escalate' so A3's dispatcher can handle it.
    effective_action_type = action_type
    if action_type not in ("escalate", "manual"):
        try:
            from backend.agent.circuit_breaker import check_failed_fix_history

            _ffr = check_failed_fix_history(app_key, action_type)
            if _ffr.should_escalate:
                log.warning(
                    "pending_fix: %s/%s has %d failed_verification in 24h — "
                    "overriding action_type to 'escalate'",
                    app_key,
                    action_type,
                    _ffr.failure_count,
                )
                effective_action_type = "escalate"
                suggested = (
                    f"[Auto-escalated: {action_type!r} failed verification "
                    f"{_ffr.failure_count}x in 24h] {suggested}"
                )[:500]
        except Exception as _gate_err:
            log.debug("failed_fix_history gate skipped: %s", _gate_err)

    try:
        from backend.core.state import StateDB

        with StateDB() as _pdb:
            _pdb.execute(
                """
                INSERT INTO pending_fixes
                    (app_key, check_name, action_type, problem, suggested_fix,
                     confidence, status, model)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(app_key, check_name, action_type)
                DO UPDATE SET
                    problem=excluded.problem,
                    suggested_fix=excluded.suggested_fix,
                    confidence=excluded.confidence,
                    status='pending',
                    model=excluded.model,
                    created_at=unixepoch(),
                    resolved_at=NULL,
                    fix_history_id=NULL
            """,
                (app_key, check_name, effective_action_type, problem, suggested, confidence, model),
            )
    except Exception as _pe:
        log.debug("pending_fixes write failed: %s", _pe)


async def _llm_diagnose(
    app_key: str,
    check_result: Any,
    logs: str,
    ollama_url: str = "http://ollama:11434",  # Docker container hostname (correct default)
    model: str = "phi4-mini",
) -> str | None:
    """Query the LLM agent for a diagnosis. Never raises — returns None on any failure.

    Orchestrates: pre-flight RAM check → prompt build (with DB context + RAG)
    → provider config → HTTP dispatch → optional RAG-expansion re-run →
    response parsing → pending_fixes upsert → human-readable diagnosis string.
    """
    if not _check_ram_for_llm(model):
        return None

    # Step 0 — pattern-library cache lookup (mirrors classifier.py:224-238).
    # Classify the raw check error offline, compute a stable hash, and query
    # fix_history for a prior successful fix.  On a hit, skip the LLM call.
    try:
        from backend.agent.classifier import classify_offline as _cls_offline
        from backend.agent.classifier import compute_signature_hash as _compute_sig
        from backend.agent.classifier import (
            current_image_digest as _cur_digest,
            derive_cache_confidence as _derive_conf,
            lookup_cached_fix as _lookup_cached_fix,
        )
        from backend.core.state import StateDB as _SDB

        _err_class = _cls_offline(check_result.message)
        _sig_hash = _compute_sig(_err_class, check_result.message, app_key)
        # Resolve the RUNNING image's digest so the cache serve is version-aware (#1003
        # TIER-D): a fix recorded against a different image version must NOT be replayed.
        # If the digest can't be resolved (empty), skip the cache entirely (fall through to
        # a fresh LLM diagnosis) — serving a version-unverifiable cached fix is the F2 hazard.
        _digest = _cur_digest(getattr(check_result, "container_name", None) or app_key)
        _served = None
        if _digest:
            with _SDB() as _cdb:
                # Shared serve helper: version- + supersede-aware (latest outcome for this
                # (signature, digest) must be 'success'). Centralised so the install + health
                # paths can never diverge (the #823 Reuse principle).
                _served = _lookup_cached_fix(_cdb, _sig_hash, image_digest=_digest)
        if _served is not None:
            _cached_fix = _served[0]
            # Evidence-ranked confidence via the shared derive_cache_confidence
            # (#823) so the install + health paths can never diverge. The health
            # path has a running container, so use its resolved digest for
            # version-aware reconciliation (the install path passes the cached
            # row's digest instead).
            _conf = _derive_conf(app_key, _sig_hash, image_digest=_digest)
            _persist_pending_fix(
                app_key,
                check_result.check_name,
                "manual",
                check_result.message,
                _cached_fix,
                _conf,
                "cache",
            )
            return f"[CACHED | confidence={_conf:.0%}] {check_result.message[:80]} — {_cached_fix}"
    except Exception:  # noqa: S110  # best-effort cache lookup; fall through to LLM if DB unavailable
        pass

    # Lazy import avoids circular import at module level (checker imports checker_llm).
    from backend.health.checker import _build_diagnosis_prompt, _extract_diagnosis

    prompt = _build_diagnosis_prompt(app_key, check_result, logs)
    provider, api_key, _model_cfg, cloud_providers = _load_provider_config()

    start = time.monotonic()
    try:
        from backend.agent.router.dispatch import route_and_dispatch  # scrub path preserved

        async with pinned_async_client(timeout=50) as client:
            raw = await route_and_dispatch(  # keeps per-provider _dispatch_llm_call scrub
                client,
                prompt,
                {"provider": provider, "api_key": api_key, "enabled": True},
                ollama_url=ollama_url,
                model=model,
                api_key=api_key,
                cloud_providers=cloud_providers,
            )
            raw = await _maybe_rag_expand(
                raw,
                prompt,
                app_key,
                ollama_url,
                model,
                logs,
            )
        elapsed = time.monotonic() - start
        _track_llm_success(elapsed, ollama_url, model)

        import json

        _stripped = raw.strip()
        clean = _stripped.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(clean)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log_routing(
            app_key, "reasoning", model, True, elapsed_ms, None, data.get("problem", "")[:120]
        )

        action_type, problem, suggested, confidence = _extract_diagnosis(data)
        _persist_pending_fix(
            app_key, check_result.check_name, action_type, problem, suggested, confidence, model
        )
        return (
            f"[{action_type.upper().replace('_', ' ')} | confidence={confidence:.0%}] "
            f"{problem} — {suggested}"
        )
    except Exception as e:
        _classify_llm_error(e, ollama_url, model)
        _log_routing(
            app_key,
            "reasoning",
            model,
            False,
            None,
            _llm_state["last_error_type"],
            _llm_state["last_error"][:120],
        )
        log.debug("LLM diagnosis skipped: %s", e)
        return None
