"""backend/api/settings.py

Global settings API — agent config, scheduler, notifications, system profile.

GET  /api/settings              — return all user-facing settings
PUT  /api/settings              — update one or more settings
GET  /api/settings/system       — live system resource profile
POST /api/settings/system/refresh — re-run system evaluation
"""

from __future__ import annotations

from typing import Any

import json

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.core.config import config

log = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SettingsPayload(BaseModel):
    # Health scheduler
    health_check_interval_secs: int | None = Field(None, ge=30, le=3600)
    # ntfy notifications
    ntfy_topic: str | None = None
    ntfy_url: str | None = None
    ntfy_enabled: bool | None = None
    # LLM agent
    llm_enabled: bool | None = None
    llm_backend: str | None = None  # "ollama" | "llamacpp"
    llm_ollama_url: str | None = None
    llm_llamacpp_url: str | None = None
    llm_model: str | None = None
    # LLM budget governance
    llm_budget: float | None = Field(None, ge=0.0)  # $/day; 0.0 = free-only
    free_tier_priority: list[str] | None = None  # provider IDs, default ["ollama"]
    # CF auto-registration
    cf_auto_register_hostnames: bool | None = None
    # Disk alerts
    disk_warn_percent: int | None = Field(None, ge=50, le=99)
    disk_error_percent: int | None = Field(None, ge=50, le=99)


class SettingsOut(BaseModel):
    # Health
    health_check_interval_secs: int
    # ntfy
    ntfy_topic: str
    ntfy_url: str
    ntfy_enabled: bool
    # LLM agent
    llm_enabled: bool
    llm_backend: str
    llm_ollama_url: str
    llm_llamacpp_url: str
    llm_model: str
    # LLM budget governance
    llm_budget: float
    free_tier_priority: list[str]
    # Scheduler status
    scheduler_running: bool
    health_last_cycle_at: str | None
    health_last_cycle_summary: dict[str, Any] | None
    # CF
    cf_auto_register_hostnames: bool
    # Disk
    disk_warn_percent: int
    disk_error_percent: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_agent_config(db: StateDB) -> dict[str, Any]:
    raw = db.get_setting("llm_agent_config")
    return json.loads(raw) if raw else {}


def _save_agent_config(db: StateDB, cfg: dict[str, Any]) -> None:
    db.set_setting("llm_agent_config", json.dumps(cfg))


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SettingsOut)
def get_settings() -> SettingsOut:
    """Return all user-facing settings with their current values."""
    from backend.health.scheduler import scheduler_status
    import datetime as _dt

    with StateDB() as db:
        interval = int(db.get_setting("health_check_interval_secs") or "30")
        ntfy_topic = db.get_setting("ntfy_topic") or "slop"
        ntfy_url = db.get_setting("ntfy_url") or "http://ntfy:80"
        ntfy_enabled = _bool(db.get_setting("ntfy_enabled"), default=True)
        agent = _load_agent_config(db)
        cf_auto = _bool(db.get_setting("cf_auto_register_hostnames"), default=True)
        disk_warn = int(db.get_setting("disk_warn_percent") or "80")
        disk_error = int(db.get_setting("disk_error_percent") or "90")
        last_at_raw = db.get_setting("health_last_cycle_at")
        last_summary_raw = db.get_setting("health_last_cycle_summary")

    sched = scheduler_status()
    last_at = _dt.datetime.fromtimestamp(int(last_at_raw)).isoformat() if last_at_raw else None
    last_summary = json.loads(last_summary_raw) if last_summary_raw else None

    # Auto-detect backend if not configured — always-on AI monitoring default
    _llm_backend = agent.get("backend") or ""
    if not _llm_backend:
        try:
            from backend.api.platform import _detect_ai_backend as _dab

            _llm_backend = _dab()
        except Exception:
            _llm_backend = "groq"
    _llm_enabled = agent.get("enabled")
    if _llm_enabled is None:
        _llm_enabled = True

    return SettingsOut(
        health_check_interval_secs=max(30, interval),  # 30s minimum — matches DNS challenge delay
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        ntfy_enabled=ntfy_enabled,
        llm_enabled=_llm_enabled,
        llm_backend=_llm_backend,
        llm_ollama_url=agent.get("ollama_url", "http://localhost:11434"),
        llm_llamacpp_url=agent.get("llamacpp_url", "http://localhost:8081"),
        llm_model=agent.get("model", "phi4-mini"),
        llm_budget=float(agent.get("llm_budget", 0.0)),
        free_tier_priority=agent.get("free_tier_priority", ["ollama"]),
        scheduler_running=sched["running"],
        health_last_cycle_at=last_at,
        health_last_cycle_summary=last_summary,
        cf_auto_register_hostnames=cf_auto,
        disk_warn_percent=disk_warn,
        disk_error_percent=disk_error,
    )


@router.put("", response_model=SettingsOut)
def update_settings(payload: SettingsPayload) -> SettingsOut:
    """Update one or more settings. Unset fields are not changed."""
    with StateDB() as db:
        agent = _load_agent_config(db)

        if payload.health_check_interval_secs is not None:
            db.set_setting(
                "health_check_interval_secs", str(max(30, payload.health_check_interval_secs))
            )  # 30s minimum

        if payload.ntfy_topic is not None:
            db.set_setting("ntfy_topic", payload.ntfy_topic)
        if payload.ntfy_url is not None:
            _ntfy = payload.ntfy_url.strip()
            if _ntfy and not _ntfy.startswith(("http://", "https://")):
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=422,
                    detail=f"ntfy_url must start with http:// or https://, got: '{_ntfy[:50]}'",
                )
            db.set_setting("ntfy_url", _ntfy)
        if payload.ntfy_enabled is not None:
            db.set_setting("ntfy_enabled", "true" if payload.ntfy_enabled else "false")

        # LLM agent config is a JSON blob
        if payload.llm_enabled is not None:
            agent["enabled"] = payload.llm_enabled
        if payload.llm_backend is not None:
            agent["backend"] = payload.llm_backend
        if payload.llm_ollama_url is not None:
            agent["ollama_url"] = payload.llm_ollama_url
        if payload.llm_llamacpp_url is not None:
            agent["llamacpp_url"] = payload.llm_llamacpp_url
        if payload.llm_model is not None:
            agent["model"] = payload.llm_model
        if payload.llm_budget is not None:
            agent["llm_budget"] = float(payload.llm_budget)
        if payload.free_tier_priority is not None:
            agent["free_tier_priority"] = payload.free_tier_priority
        _save_agent_config(db, agent)

        if payload.cf_auto_register_hostnames is not None:
            db.set_setting(
                "cf_auto_register_hostnames",
                "true" if payload.cf_auto_register_hostnames else "false",
            )

        if payload.disk_warn_percent is not None:
            db.set_setting("disk_warn_percent", str(payload.disk_warn_percent))
        if payload.disk_error_percent is not None:
            db.set_setting("disk_error_percent", str(payload.disk_error_percent))

    log.info("Settings updated: %s", payload.model_dump(exclude_none=True))
    return get_settings()


@router.get("/system")
def get_system_profile() -> dict[str, Any]:
    """Return system profile — stored fingerprint + live RAM refresh.

    Merges the profile stored at prereqs time with current RAM availability
    so callers always get fresh memory figures without a full re-evaluation.
    Falls back to a live evaluate_system() call on first boot.
    """
    import json as _json
    from backend.core.state import StateDB as _SDB

    stored: dict[str, Any] = {}
    try:
        with _SDB() as db:
            raw = db.get_setting("system_profile")
            if raw:
                stored = _json.loads(raw)
    except Exception:  # noqa: S110  # best-effort profile load; proceed with empty profile
        pass

    # Refresh live RAM into stored profile
    try:
        from backend.core.system_eval import read_meminfo

        mem = read_meminfo()
        total_mb = mem.get("MemTotal", 0) // 1024
        avail_mb = mem.get("MemAvailable", 0) // 1024
        stored.setdefault("ram", {})
        stored["ram"]["total_gb"] = round(total_mb / 1024, 1)
        stored["ram"]["available_gb"] = round(avail_mb / 1024, 1)
        stored["total_ram_mb"] = total_mb
        stored["available_ram_mb"] = avail_mb
    except Exception:  # noqa: S110  # best-effort live RAM read; unavailable in some container environments
        pass

    if stored:
        # Flatten for backward compat with existing callers (context_assembler etc)
        ram = stored.get("ram", {})
        cpu = stored.get("cpu", {})
        gpu_list = stored.get("gpu", [])
        gpu = gpu_list[0] if gpu_list else {}
        docker = stored.get("docker", {})
        os_info = stored.get("os", {})
        user = stored.get("user", {})
        return {
            # Legacy keys
            "cpu_cores": cpu.get("cores", 0),
            "cpu_model": cpu.get("model", ""),
            "architecture": cpu.get("arch", os_info.get("arch", "")),
            "total_ram_gb": ram.get("total_gb", 0),
            "free_ram_gb": ram.get("available_gb", 0),
            "headroom_ram_gb": ram.get("headroom_gb", 0),
            "recommended_llm_model": stored.get("recommended_model", ""),
            "available_llm_models": [],
            "llm_warning": stored.get("llm_warning"),
            "measured_at": stored.get("collected_at", 0),
            "total_ram_mb": stored.get("total_ram_mb", 0),
            "available_ram_mb": stored.get("available_ram_mb", 0),
            # New rich fields
            "os": os_info,
            "cpu": cpu,
            "ram": ram,
            "gpu": gpu,
            "disks": stored.get("disks", []),
            "docker": docker,
            "user": user,
            "timezone": stored.get("timezone", ""),
            "server_ip": stored.get("server_ip", ""),
            "containers_running": docker.get("containers_running", 0),
            "note": "Profile collected at prereqs stage, RAM refreshed live.",
        }

    # Fallback: live evaluation (first boot before prereqs ran)
    from backend.core.system_eval import evaluate_system
    from backend.core.state import StateDB

    with StateDB() as db:
        p = db.get_platform()
        installed_keys = [a.key for a in db.get_all_apps()]

    profile = evaluate_system(
        selected_app_keys=installed_keys,
        config_root=getattr(p, "config_root", None) or "/",
        media_root=getattr(p, "media_root", None) or "/",
    )

    return {
        "cpu_cores": profile.cpu_cores,
        "cpu_model": profile.cpu_model,
        "architecture": profile.architecture,
        "total_ram_gb": round(profile.total_ram_mb / 1024, 1),
        "free_ram_gb": round(profile.free_ram_mb / 1024, 1),
        "headroom_ram_gb": round(profile.headroom_ram_mb / 1024, 1),
        "recommended_llm_model": profile.recommended_model,
        "available_llm_models": profile.available_models,
        "llm_warning": profile.llm_warning,
        "measured_at": profile.measured_at,
        "total_ram_mb": profile.total_ram_mb,
        "available_ram_mb": profile.free_ram_mb,
        "os": {
            "distro": profile.os_distro,
            "version": profile.os_version,
            "arch": profile.os_arch,
            "kernel": profile.kernel_version,
        },
        "cpu": {"model": profile.cpu_model, "cores": profile.cpu_cores, "avx2": profile.avx2},
        "ram": {
            "total_gb": round(profile.total_ram_mb / 1024, 1),
            "available_gb": round(profile.free_ram_mb / 1024, 1),
        },
        "gpu": (
            {
                "vendor": profile.gpu_vendor,
                "model": profile.gpu_name,
                "vram_gb": round(profile.gpu_vram_mb / 1024, 1),
                "inference_capable": profile.gpu_inference_capable,
            }
            if profile.gpu_name
            else {}
        ),
        "docker": {
            "engine": profile.docker_version,
            "compose": profile.compose_version,
            "containers_running": profile.containers_running,
        },
        "timezone": profile.timezone,
        "server_ip": profile.server_ip,
        "user": {"puid": profile.puid, "pgid": profile.pgid, "username": profile.puid_username},
        "note": "Live evaluation — run prereqs to store a persistent profile.",
    }


# ── Secrets (env file) ──────────────────────────────────────────────────────

# Keys exposed in the Secrets UI — others are omitted for safety
_SECRETS_KEYS = [
    "CF_DNS_API_TOKEN",
    "CF_ACCOUNT_ID",
    "CF_TUNNEL_ID",
    "CF_ZONE_ID",
    "POSTGRES_PASSWORD",
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
]


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


@router.get("/secrets")
def get_secrets() -> dict[str, Any]:
    """Return current .env values for the secrets UI.
    Sensitive values are masked — only first/last 4 chars shown.
    """
    env = _read_env_file()
    result: dict[str, dict[str, Any]] = {}
    sensitive = {
        "CF_DNS_API_TOKEN",
        "POSTGRES_PASSWORD",
        "KOMODO_JWT_SECRET",
        "KOMODO_PASSKEY",
        "DOCKHAND_DB_PASSWORD",
        "PLEX_CLAIM",
    }

    for key in _SECRETS_KEYS:
        val = env.get(key, "")
        masked = val
        if key in sensitive and len(val) > 8:
            masked = val[:4] + "•" * (len(val) - 8) + val[-4:]
        elif key in sensitive and val:
            masked = "•" * len(val)
        result[key] = {
            "value": masked,
            "is_set": bool(val),
            "is_sensitive": key in sensitive,
        }
    return {
        "secrets": result,
        "env_file": str(config.env_file),
    }


class SecretUpdate(BaseModel):
    updates: dict[str, str] = {}

    @classmethod
    def from_flat(cls, data: dict[str, Any]) -> SecretUpdate:
        """Accept either {updates: {...}} or flat {KEY: VALUE} format."""
        if "updates" in data and isinstance(data["updates"], dict):
            return cls(**data)
        return cls(updates=data)

    model_config = {"extra": "allow"}

    def model_post_init(self, __context: Any) -> None:
        # If extra fields were passed (flat format), move them to updates
        extra = {k: v for k, v in (self.__pydantic_extra__ or {}).items() if k != "updates"}
        if extra:
            self.updates = {**self.updates, **extra}


@router.put("/secrets")
def update_secrets(payload: SecretUpdate) -> dict[str, Any]:
    """Update one or more values in the .env file.
    Only keys in the allowed list can be updated.
    After saving, the service must be restarted for changes to take effect
    (the UI prompts the user to restart).
    """
    from fastapi import HTTPException

    disallowed = [k for k in payload.updates if k not in _SECRETS_KEYS]
    if disallowed:
        raise HTTPException(
            status_code=422,
            detail=f"Keys not allowed in secrets UI: {', '.join(disallowed)}",
        )
    try:
        _write_env_file(payload.updates)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {"ok": True, "updated": list(payload.updates.keys())}


# ── AI safety settings ──────────────────────────────────────────────────────


@router.get("/ai-safety")
def get_ai_safety() -> dict[str, Any]:
    """Return current AI safety levels for all action types."""
    from backend.core.ai_safety import get_all_safety_levels

    return {"levels": get_all_safety_levels()}


class SafetyUpdate(BaseModel):
    action_type: str
    level: str  # observe | suggest | act


@router.put("/ai-safety")
def update_ai_safety(payload: SafetyUpdate) -> dict[str, Any]:
    """Update the safety level for an action type.

    Safety levels:
      observe  — AI can only read and report (never suggests actions)
      suggest  — AI suggests actions, user must approve before execution (default)
      act      — AI executes the action automatically (explicit opt-in only)
    """
    from backend.core.ai_safety import set_safety_level
    from fastapi import HTTPException

    try:
        set_safety_level(payload.action_type, payload.level)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"ok": True, "action_type": payload.action_type, "level": payload.level}


# ── Pre-approval policy (N5 — tier x scope) ──────────────────────────────────


@router.get("/preapproval")
def get_preapproval_policy() -> dict[str, Any]:
    """Return the effective tier x scope pre-approval policy (operational plan §W5).

    Surfaces the per-tier global defaults, per-app overrides, and the immutable fact
    that T3 (irreversible/always-ask) can never be pre-approved (safety invariant 8).
    """
    from backend.agent.policy import effective_policy_view

    return effective_policy_view()


class TierDefaultUpdate(BaseModel):
    tier: int = Field(..., ge=0, le=3)
    pre_approved: bool


@router.put("/preapproval/tier")
def update_preapproval_tier(payload: TierDefaultUpdate) -> dict[str, Any]:
    """Set the GLOBAL pre-approval default for a tier.

    Refuses T3 (always-ask) — no toggle can pre-approve an irreversible action.
    """
    from backend.agent.policy import set_tier_default
    from backend.agent.types import ActionTier
    from fastapi import HTTPException

    try:
        set_tier_default(ActionTier.from_value(payload.tier), payload.pre_approved)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    from backend.agent.policy import effective_policy_view

    return effective_policy_view()


class AppOverrideUpdate(BaseModel):
    app_key: str
    tier: int = Field(..., ge=0, le=3)
    pre_approved: bool


@router.put("/preapproval/app")
def update_preapproval_app(payload: AppOverrideUpdate) -> dict[str, Any]:
    """Set a PER-APP pre-approval override for (app_key, tier).

    Per-app blast-radius scoping (invariant 6). Refuses T3.
    """
    from backend.agent.policy import set_app_override
    from backend.agent.types import ActionTier
    from fastapi import HTTPException

    try:
        set_app_override(payload.app_key, ActionTier.from_value(payload.tier), payload.pre_approved)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    from backend.agent.policy import effective_policy_view

    return effective_policy_view()


@router.delete("/preapproval/app/{app_key}")
def clear_preapproval_app(app_key: str, tier: int | None = None) -> dict[str, Any]:
    """Remove a per-app override — one tier (``?tier=N``) or the whole app."""
    from backend.agent.policy import clear_app_override, effective_policy_view
    from backend.agent.types import ActionTier

    clear_app_override(app_key, ActionTier.from_value(tier) if tier is not None else None)
    return effective_policy_view()


# ── Cloud LLM settings ──────────────────────────────────────────────────────


@router.get("/cloud-llm")
def get_cloud_llm_settings() -> dict[str, Any]:
    """Return cloud LLM provider configuration and cost data."""
    from backend.core.cloud_llm import PROVIDERS, DEFAULT_CASCADE
    from backend.core.state import StateDB
    import datetime

    with StateDB() as db:
        cascade_str = db.get_setting("cloud_llm_cascade") or ",".join(DEFAULT_CASCADE)
        monthly_limit = float(db.get_setting("cloud_llm_monthly_limit_usd") or "1.00")

        # Get configured provider keys (those with API keys in .env)
        from backend.core.config import config as _cfg

        configured: list[str] = []
        if _cfg.env_file.exists():
            env_lines = _cfg.env_file.read_text()
            for key, meta in PROVIDERS.items():
                env_key = meta["env_key"]
                if f"{env_key}=" in env_lines:
                    val = ""
                    for line in env_lines.splitlines():
                        if line.strip().startswith(f"{env_key}="):
                            val = line.strip().split("=", 1)[1].strip()
                    if val:
                        configured.append(key)

    # Monthly spend data
    first_of_month = int(
        datetime.datetime(datetime.date.today().year, datetime.date.today().month, 1).timestamp()
    )

    try:
        with StateDB() as db:
            spend_rows = db.execute(
                """SELECT provider, SUM(cost_usd) as spend, SUM(total_tokens) as tokens,
                   COUNT(*) as calls FROM cloud_llm_usage
                   WHERE created_at >= ? GROUP BY provider""",
                (first_of_month,),
            ).fetchall()
            recent = db.execute(
                """SELECT provider, model, total_tokens, cost_usd, purpose, created_at
                   FROM cloud_llm_usage ORDER BY created_at DESC LIMIT 10"""
            ).fetchall()
        spend_by_provider = {
            r["provider"]: {"spend": r["spend"], "tokens": r["tokens"], "calls": r["calls"]}
            for r in spend_rows
        }
        total_spend = sum(r["spend"] for r in spend_rows)
        recent_calls = [dict(r) for r in recent]
    except Exception:
        spend_by_provider = {}
        total_spend = 0.0
        recent_calls = []

    return {
        "providers": {k: {**v, "configured": k in configured} for k, v in PROVIDERS.items()},
        "cascade": cascade_str.split(","),
        "monthly_limit_usd": monthly_limit,
        "total_spend_this_month": total_spend,
        "spend_by_provider": spend_by_provider,
        "recent_calls": recent_calls,
    }


class CloudLLMUpdate(BaseModel):
    cascade: list[str] | None = None
    monthly_limit_usd: float | None = None


@router.put("/cloud-llm")
def update_cloud_llm_settings(payload: CloudLLMUpdate) -> dict[str, Any]:
    """Update cloud LLM cascade and cost limit settings."""
    from backend.core.cloud_llm import PROVIDERS
    from backend.core.state import StateDB

    with StateDB() as db:
        if payload.cascade is not None:
            unknown = [p for p in payload.cascade if p not in PROVIDERS]
            if unknown:
                from fastapi import HTTPException

                raise HTTPException(422, f"Unknown providers: {unknown}")
            db.set_setting("cloud_llm_cascade", ",".join(payload.cascade))
        if payload.monthly_limit_usd is not None:
            if payload.monthly_limit_usd < 0:
                from fastapi import HTTPException

                raise HTTPException(422, "Monthly limit must be >= 0")
            db.set_setting("cloud_llm_monthly_limit_usd", str(round(payload.monthly_limit_usd, 2)))
    return {"ok": True}


# ── Traefik settings ────────────────────────────────────────────────────────


class TraefikSettings(BaseModel):
    image_tag: str | None = None  # e.g. "v3.2", "latest"
    dashboard_port: int | None = None  # host port for dashboard (default 8081)


@router.get("/traefik")
def get_traefik_settings() -> dict[str, Any]:
    """Return current Traefik image tag and dashboard port."""
    with StateDB() as db:
        image_tag = db.get_setting("traefik_image_tag") or "v3.2"
        dashboard_port = int(db.get_setting("traefik_dashboard_port") or "8081")
    return {"image_tag": image_tag, "dashboard_port": dashboard_port}


@router.put("/traefik")
def update_traefik_settings(payload: TraefikSettings) -> dict[str, Any]:
    """Update Traefik image tag and/or dashboard port.

    Changes take effect on next Traefik deployment or restart.
    The compose fragment is regenerated with the new values.
    """
    with StateDB() as db:
        if payload.image_tag is not None:
            tag = payload.image_tag.strip()
            if not tag:
                from fastapi import HTTPException

                raise HTTPException(422, "image_tag cannot be empty")
            db.set_setting("traefik_image_tag", tag)
        if payload.dashboard_port is not None:
            port = payload.dashboard_port
            if not (1024 <= port <= 65535):
                from fastapi import HTTPException

                raise HTTPException(422, "dashboard_port must be between 1024 and 65535")
            db.set_setting("traefik_dashboard_port", str(port))
    return {"ok": True}
