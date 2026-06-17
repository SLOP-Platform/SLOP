"""backend/agent/recovery_probes/credential.py — credential validity GROUND probe.

Probe 4: credential_validity
    If the app manifest declares ``auto_secrets``, each referenced env-var key
    must be present and non-empty in the platform ``.env`` file.
    DRIFT if any declared secret is absent or empty.
    INDETERMINATE if the ``.env`` file cannot be read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.agent.spine import Finding, Verdict

# Minimum entropy bytes for a generated secret (token_hex(N) → 2*N hex chars)
_SECRET_MIN_LEN = 16  # 8 bytes of entropy minimum; anything shorter is suspicious


def _read_env_file(env_path: str) -> dict[str, str] | None:
    """Read a .env file into a key→value dict.

    Returns None if the file is unreadable (yields INDETERMINATE).
    """
    try:
        lines = Path(env_path).read_text(encoding="utf-8").splitlines()
    except Exception:  # best-effort; caller handles None
        return None

    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            k, _, v = stripped.partition("=")
            result[k.strip()] = v.strip()
    return result


def _extract_secret_keys(auto_secrets: Any) -> list[str]:
    """Normalise auto_secrets entries (``{key:, length:}`` dicts or bare strings) to key names."""
    secret_keys: list[str] = []
    for entry in auto_secrets:
        if isinstance(entry, dict):
            k = entry.get("key", "")
        elif isinstance(entry, str):
            k = entry
        else:
            k = ""
        if k:
            secret_keys.append(k)
    return secret_keys


def _resolve_credential_env_path(env_path: str) -> str:
    """Resolve the .env path, falling back to ``backend.core.config`` when unset."""
    if env_path:
        return env_path
    try:
        from backend.core.config import config as _cfg

        return str(_cfg.env_file)
    except Exception:  # config import may fail in test environments
        return ""


def _classify_secrets(
    secret_keys: list[str], env_vars: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Split declared secret keys into (missing, empty, below-min-length) buckets."""
    missing: list[str] = []
    empty: list[str] = []
    short: list[str] = []
    for key_name in secret_keys:
        val = env_vars.get(key_name)
        if val is None:
            missing.append(key_name)
        elif val == "":
            empty.append(key_name)
        elif len(val) < _SECRET_MIN_LEN:
            short.append(f"{key_name}(len={len(val)})")
    return missing, empty, short


def _probe_credential_validity(app: Any, env_path: str = "") -> Finding | None:
    """GROUND: declared auto_secrets are present and well-formed in the .env file.

    Checks that every key listed in the manifest's ``auto_secrets`` field:
      - exists in the platform ``.env`` file
      - is non-empty
      - meets a minimum length threshold (prevents stub/placeholder values)

    Returns None when the manifest declares no ``auto_secrets`` (omit).
    Returns INDETERMINATE when the ``.env`` file cannot be read.
    Returns DRIFT when any secret is absent, empty, or below the length threshold.
    Returns VERIFIED when all declared secrets are present and well-formed.
    """
    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.credential_validity.{app_key}"
    physics = f"auto_secrets env-var presence + length for app {app_key}"

    secret_keys = _extract_secret_keys(getattr(app, "auto_secrets", None) or [])
    if not secret_keys:
        return None  # no secrets declared — omit finding

    resolved_env_path = _resolve_credential_env_path(env_path)
    if not resolved_env_path or not Path(resolved_env_path).exists():
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="credential probe: .env file absent or path unknown",
            detail=f"env_path={resolved_env_path!r}",
        )

    env_vars = _read_env_file(resolved_env_path)
    if env_vars is None:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="credential probe: .env file unreadable",
            detail=f"env_path={resolved_env_path!r}",
        )

    missing, empty, short = _classify_secrets(secret_keys, env_vars)

    if missing:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"declared secret(s) absent from .env: {', '.join(missing)}",
            detail=f"missing={missing} env_path={resolved_env_path!r}",
        )
    if empty:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"declared secret(s) present but empty in .env: {', '.join(empty)}",
            detail=f"empty={empty} env_path={resolved_env_path!r}",
        )
    if short:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"declared secret(s) below minimum length ({_SECRET_MIN_LEN} chars): {', '.join(short)}",
            detail=f"short={short} min_len={_SECRET_MIN_LEN} env_path={resolved_env_path!r}",
        )

    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"all {len(secret_keys)} declared secret(s) present and well-formed",
        detail=f"checked={secret_keys}",
    )
