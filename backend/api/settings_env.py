"""backend/api/settings_env.py — .env file I/O + secret-key registries for the Settings API.

Extracted from ``backend/api/settings.py`` (#1258 decomposition: the god-object was 863L > the
800 api_routers cap). This module owns the cohesive ``.env`` read/write + value-quoting helpers and
the two secret-key registries the Secrets UI masks against. ``backend/api/settings.py`` RE-IMPORTS
these names (so ``settings.<name>`` and the tests' ``monkeypatch.setattr(settings, "_read_env_file",
…)`` keep working) — the route handlers + ``ensure_control_plane_token_provisioned`` stay there.

No business logic moved — only the .env primitives + the registries. Imports only ``config`` (no
StateDB / router), so it carries no FastAPI or DB coupling.
"""

from __future__ import annotations

from backend.core.config import config

_SECRETS_KEYS = [
    "CF_DNS_API_TOKEN",
    "CF_ACCOUNT_ID",
    "CF_TUNNEL_ID",
    "CF_ZONE_ID",
    "POSTGRES_PASSWORD",
    "MARIADB_PASSWORD",
    "MARIADB_ROOT_PASSWORD",
    "PLEX_CLAIM",
    "KOMODO_JWT_SECRET",
    "KOMODO_PASSKEY",
    "DOCKHAND_DB_PASSWORD",
    "TZ",
    "CONFIG_ROOT",
    "MEDIA_ROOT",
    "MS_PORT",
    "DOCKER_GID",
    "HF_TOKEN",
    # #976 control-plane auth token (.env key SLOP_CONTROL_PLANE_TOKEN). Listed here so the
    # Secrets UI surfaces it (is_set indicator + an operator-set channel for installs the
    # auto-provisioner did not reach) and so PUT /secrets accepts it. Masked in GET /secrets
    # via _SENSITIVE_SECRET_KEYS below (L3 §8: never returned in clear); never in SettingsOut.
    "SLOP_CONTROL_PLANE_TOKEN",
]

#: Subset of _SECRETS_KEYS whose VALUES are credentials/tokens and must be masked in
#: GET /secrets (never returned in clear). Co-located with _SECRETS_KEYS so the two
#: lists can't silently drift — adding a credential key above without listing it here
#: would leak it unmasked (the HF_TOKEN regression this constant prevents). The omitted
#: keys (CF_ACCOUNT_ID/TUNNEL_ID/ZONE_ID, TZ, paths, ports, GID) are identifiers/config,
#: not credentials.
_SENSITIVE_SECRET_KEYS = frozenset(
    {
        "CF_DNS_API_TOKEN",
        "POSTGRES_PASSWORD",
        "MARIADB_PASSWORD",
        "MARIADB_ROOT_PASSWORD",
        "KOMODO_JWT_SECRET",
        "KOMODO_PASSKEY",
        "DOCKHAND_DB_PASSWORD",
        "PLEX_CLAIM",
        "HF_TOKEN",  # Hugging Face Bearer token for gated model downloads (was leaked unmasked)
        # #976 §8 (L3, load-bearing): the control-plane token must NEVER be returned in clear —
        # an unauthenticated local READ caller could otherwise retrieve it and satisfy enforce,
        # making the token worthless over the PrivateIP gate. Masked first/last-4 in GET /secrets.
        "SLOP_CONTROL_PLANE_TOKEN",
    }
)

#: .env key + StateDB setting name for the #976 control-plane auth token. The env key is the
#: primary (loaded into os.environ via the systemd EnvironmentFile); the StateDB setting is
#: auth.py's fallback. Kept in sync with backend/api/auth.py (_ENV_TOKEN / _SETTING_TOKEN).
_CONTROL_PLANE_ENV_KEY = "SLOP_CONTROL_PLANE_TOKEN"  # env-var NAME, not a secret value
_CONTROL_PLANE_SETTING_KEY = "control_plane_token"  # setting NAME, not a secret value


def _unquote_env_val(val: str) -> str:
    """Strip surrounding single or double quotes from a .env value."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    return val


def _quote_env_val(val: str) -> str:
    """Single-quote values containing $ so Docker Compose doesn't interpolate them."""
    if "$" in val and not (val.startswith("'") and val.endswith("'")):
        return "'" + val + "'"
    return val


def _sanitize_env_val(val: str) -> str:
    """Strip newlines (injection guard) then quote if needed."""
    v = str(val).replace(chr(10), "").replace(chr(13), "")
    return _quote_env_val(v)


def _read_env_file() -> dict[str, str]:
    """Read the .env file and return a {key: value} dict."""
    env_path = config.env_file
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = _unquote_env_val(val.strip())
    return result


def _write_env_file(updates: dict[str, str]) -> None:
    """Update specific keys in the .env file, preserving all other content."""
    env_path = config.env_file
    # Create .env if it doesn't exist — first-run or test environment
    if not env_path.exists():
        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.touch(mode=0o600)
        except Exception as e:
            raise FileNotFoundError(
                f"Cannot create .env at {env_path}: {e}. "
                f"Run deploy.sh to initialize the environment file."
            ) from e

    lines = env_path.read_text().splitlines(keepends=True)
    written_keys: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={_sanitize_env_val(updates[key])}\n")
            written_keys.add(key)
        else:
            new_lines.append(line)

    # Append any keys that weren't already in the file
    for key, val in updates.items():
        if key not in written_keys:
            new_lines.append(f"{key}={_sanitize_env_val(val)}\n")

    env_path.write_text("".join(new_lines))


__all__ = [
    "_CONTROL_PLANE_ENV_KEY",
    "_CONTROL_PLANE_SETTING_KEY",
    "_SECRETS_KEYS",
    "_SENSITIVE_SECRET_KEYS",
    "_quote_env_val",
    "_read_env_file",
    "_sanitize_env_val",
    "_unquote_env_val",
    "_write_env_file",
]
