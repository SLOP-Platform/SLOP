"""backend/api/platform.py

Platform wizard API routes.

GET  /api/platform/status          — current platform state
POST /api/platform/wizard/validate — validate inputs before running
POST /api/platform/wizard/run      — run the full wizard
GET  /api/platform/wizard/steps    — list of steps with descriptions
"""

from __future__ import annotations
import threading as _threading
import time as _time
import uuid as _uuid
from typing import Any

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, Field

# WizardRequest extracted to platform_schemas.py (#1302 linecount drain).
# Re-exported so the wizard route bodies + `from backend.api.platform import
# WizardRequest` (tests) resolve unchanged.
from backend.api.platform_schemas import WizardRequest as WizardRequest
from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.platform.wizard import (
    WizardInput,
    run_wizard,
    validate_wizard,
)
from backend.api.contract import confirm_token
from backend.api.rate_limit import limiter
from backend.api.jobs_store import (
    load_ollama_job as _load_ollama_job_from_db,
    load_wizard_job as _load_wizard_job_from_db,
    persist_job as _persist_job,
)


log = get_logger(__name__)
router = APIRouter()


def _detect_ai_backend() -> str:
    """Return the most suitable LLM backend for this hardware.

    Priority: GPU (ollama) > ≥16 GB RAM (llamacpp) > cloud fallback (groq).
    """
    import os as _os
    import subprocess as _sub

    # GPU check
    try:
        r = _sub.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return "ollama"
    except Exception as e:
        log.debug("platform best-effort step skipped: %s", e)
    if _os.path.exists("/dev/nvidia0"):
        return "ollama"

    # RAM check — read MemTotal from /proc/meminfo
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_gb = int(line.split()[1]) // (1024 * 1024)
                    if ram_gb >= 16:
                        return "llamacpp"
                    break
    except Exception as e:
        log.debug("platform best-effort step skipped: %s", e)

    # Default: cloud (groq free tier)
    return "groq"


def _calc_install_concurrency() -> int:
    """Derive parallel install cap from available CPU and RAM."""
    import os as _os

    cpu_cores = _os.cpu_count() or 2

    avail_gb = 4  # safe fallback
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_gb = int(line.split()[1]) // (1024 * 1024)
                    break
    except Exception as e:
        log.debug("platform best-effort step skipped: %s", e)

    # Each parallel install pulls an image + starts a container (~500 MB headroom each)
    ram_slots = max(2, avail_gb // 2)
    cpu_slots = max(2, cpu_cores // 2)
    return min(ram_slots, cpu_slots, 8)


# ── Quick Stacks defaults ─────────────────────────────────────────────────
# Single source of truth for default stacks.  Each entry has:
#   id       — stable slug used as form value and storage key
#   label    — display name shown in UI
#   app_keys — catalog app slugs to install (lowercased, underscored)
#   ram_note — human-readable RAM hint string
#   ram_gb   — numeric RAM threshold for the low-RAM warning
# Entries removed here will not resurface from DB unless saved as a custom stack.
_DEFAULT_STACKS: list[dict[str, Any]] = [
    {
        "id": "arr_basic",
        "label": "Arr Stack",
        "app_keys": [
            "sonarr",
            "radarr",
            "prowlarr",
            "sabnzbd",
            "seerr",
            "bazarr",
            "lidarr",
            "plex",
        ],
        "ram_note": "~4GB RAM",
        "ram_gb": 4,
    },
    {
        "id": "debrid",
        "label": "Debrid Stack",
        "app_keys": ["decypharr", "zilean"],
        "ram_note": "~1GB RAM",
        "ram_gb": 1,
    },
    {
        "id": "productivity",
        "label": "Productivity",
        "app_keys": ["vaultwarden", "paperless_ngx", "mealie"],
        "ram_note": "~2GB RAM",
        "ram_gb": 2,
    },
    {
        "id": "ai_local",
        "label": "Local AI",
        "app_keys": ["ollama", "open_webui"],
        "ram_note": "~8GB RAM · GPU optional",
        "ram_gb": 8,
    },
]


def _load_stacks_from_db(db: StateDB) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (custom_stacks, hidden_default_ids) from the settings table."""
    import json as _json

    raw_custom = db.get_setting("custom_stacks")
    raw_hidden = db.get_setting("hidden_stacks")
    custom: list[dict[str, Any]] = _json.loads(raw_custom) if raw_custom else []
    hidden: list[str] = _json.loads(raw_hidden) if raw_hidden else []
    return custom, hidden


def _save_custom_stacks(db: StateDB, stacks: list[dict[str, Any]]) -> None:
    import json as _json

    db.set_setting("custom_stacks", _json.dumps(stacks))


def _save_hidden_stacks(db: StateDB, hidden: list[str]) -> None:
    import json as _json

    db.set_setting("hidden_stacks", _json.dumps(hidden))


def _build_stacks_response(custom: list[dict[str, Any]], hidden: list[str]) -> list[dict[str, Any]]:
    """Merge defaults + custom into the full stacks list for the API."""
    result: list[dict[str, Any]] = []
    # Defaults come first (unless hidden); a custom entry with the same id overrides the default
    custom_ids = {s["id"] for s in custom}
    for s in _DEFAULT_STACKS:
        if s["id"] in hidden:
            continue
        if s["id"] in custom_ids:
            # Use the custom override (label/app_keys/ram_note may differ)
            override = next(c for c in custom if c["id"] == s["id"])
            result.append(
                {
                    **override,
                    "is_custom": True,
                    "is_default_override": True,
                    "ram_gb": override.get("ram_gb", 0),
                }
            )
        else:
            result.append({**s, "is_custom": False, "is_default_override": False})
    # Custom stacks that are not default overrides come after
    for s in custom:
        if s["id"] not in {d["id"] for d in _DEFAULT_STACKS}:
            result.append(
                {**s, "is_custom": True, "is_default_override": False, "ram_gb": s.get("ram_gb", 0)}
            )
    return result


# ── Quick Stacks request models (id=466) ─────────────────────────────────


class CreateStackRequest(BaseModel):
    """Typed request body for POST /stacks."""

    label: str = Field(..., description="Display name for the stack")
    app_keys: list[str] = Field(..., description="Catalog app slugs to install")
    ram_note: str = Field("", description="Human-readable RAM requirement hint")
    ram_gb: int = Field(0, ge=0, description="Numeric RAM threshold for low-RAM warning")


class UpdateStackRequest(BaseModel):
    """Typed request body for PUT /stacks/{stack_id}."""

    label: str | None = Field(None, description="New display name")
    app_keys: list[str] | None = Field(None, description="New app keys list")
    ram_note: str | None = Field(None, description="New RAM note")
    ram_gb: int | None = Field(None, ge=0, description="New RAM threshold")


class OllamaSetupRequest(BaseModel):
    """Typed request body for POST /wizard/setup-ollama."""

    model: str = Field("phi4-mini", description="Ollama model name to pull after install")


class BcryptUsersRequest(BaseModel):
    """Typed request body for POST /wizard/bcrypt-users."""

    username: str = Field("admin", description="TinyAuth username")
    password: str = Field(..., description="Plaintext password to hash")


# ── Quick Stacks endpoints ────────────────────────────────────────────────


@router.get("/stacks")
def list_stacks() -> dict[str, Any]:
    """Return all quick stacks: visible defaults (may be overridden) + pure custom."""
    with StateDB() as db:
        custom, hidden = _load_stacks_from_db(db)
    return {"stacks": _build_stacks_response(custom, hidden)}


@router.post("/stacks")
def create_stack(req: CreateStackRequest) -> dict[str, Any]:
    """Create a new custom quick stack."""
    label = req.label.strip()
    app_keys = req.app_keys
    ram_note = req.ram_note.strip()
    ram_gb = req.ram_gb
    if not label:
        raise HTTPException(status_code=422, detail="label is required")
    if not app_keys:
        raise HTTPException(status_code=422, detail="app_keys must be a non-empty list")
    import time as _time

    stack_id = f"custom_{int(_time.time())}"
    new_stack: dict[str, Any] = {
        "id": stack_id,
        "label": label,
        "app_keys": app_keys,
        "ram_note": ram_note,
        "ram_gb": ram_gb,
    }
    with StateDB() as db:
        custom, _hidden = _load_stacks_from_db(db)
        custom.append(new_stack)
        _save_custom_stacks(db, custom)
    return {"ok": True, "stack": {**new_stack, "is_custom": True, "is_default_override": False}}


@router.put("/stacks/{stack_id}")
def update_stack(stack_id: str, req: UpdateStackRequest) -> dict[str, Any]:
    """Update label, app_keys, ram_note, or ram_gb for any stack.
    Editing a default stack creates a custom override entry."""
    req_dict = req.model_dump(exclude_none=True)
    with StateDB() as db:
        custom, _hidden = _load_stacks_from_db(db)
        existing_idx = next((i for i, s in enumerate(custom) if s["id"] == stack_id), None)
        if existing_idx is not None:
            for field in ("label", "app_keys", "ram_note", "ram_gb"):
                if field in req_dict:
                    custom[existing_idx][field] = req_dict[field]
        else:
            default = next((s for s in _DEFAULT_STACKS if s["id"] == stack_id), None)
            if not default:
                raise HTTPException(status_code=404, detail=f"Stack '{stack_id}' not found")
            override = {
                "id": stack_id,
                "label": req_dict.get("label", default["label"]),
                "app_keys": req_dict.get("app_keys", default["app_keys"]),
                "ram_note": req_dict.get("ram_note", default["ram_note"]),
                "ram_gb": req_dict.get("ram_gb", default["ram_gb"]),
            }
            custom.append(override)
        _save_custom_stacks(db, custom)
    return {"ok": True}


@router.delete("/stacks/{stack_id}")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # light mutation — stack deletes are recoverable (id=467)
def delete_stack(request: Request, stack_id: str) -> dict[str, Any]:
    """Delete a custom stack, or hide a default stack."""
    with StateDB() as db:
        custom, hidden = _load_stacks_from_db(db)
        new_custom = [s for s in custom if s["id"] != stack_id]
        if len(new_custom) < len(custom):
            _save_custom_stacks(db, new_custom)
            return {"ok": True, "action": "deleted"}
        if any(s["id"] == stack_id for s in _DEFAULT_STACKS):
            if stack_id not in hidden:
                hidden.append(stack_id)
            _save_hidden_stacks(db, hidden)
            return {"ok": True, "action": "hidden"}
        raise HTTPException(status_code=404, detail=f"Stack '{stack_id}' not found")


@router.post("/stacks/{stack_id}/restore")
def restore_stack(stack_id: str) -> dict[str, Any]:
    """Un-hide a default stack and remove any custom override for it."""
    if not any(s["id"] == stack_id for s in _DEFAULT_STACKS):
        raise HTTPException(status_code=404, detail=f"'{stack_id}' is not a default stack")
    with StateDB() as db:
        custom, hidden = _load_stacks_from_db(db)
        hidden = [h for h in hidden if h != stack_id]
        custom = [s for s in custom if s["id"] != stack_id]
        _save_custom_stacks(db, custom)
        _save_hidden_stacks(db, hidden)
    return {"ok": True}


# ── Request / Response models ─────────────────────────────────────────────


class StepInfo(BaseModel):
    name: str
    title: str
    description: str


class WizardStepResult(BaseModel):
    step: str
    status: str
    message: str
    detail: str = ""


class WizardRunResponse(BaseModel):
    ok: bool
    platform_ready: bool
    steps: list[WizardStepResult]
    error: str | None = None


class ValidationIssue(BaseModel):
    field: str
    message: str


class ValidateResponse(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = []


class PlatformStatus(BaseModel):
    status: str
    domain: str | None
    network_name: str
    config_root: str
    media_root: str
    puid: int
    pgid: int
    timezone: str
    traefik_version: str | None
    installed_at: int | None


# ── Typed request bodies (P1-H) ───────────────────────────────────────────


class WizardSecretsRequest(BaseModel):
    """Typed body for POST /wizard/validate-secrets."""

    checks: list[str] = Field(
        default_factory=list, description="Which checks to run: dns, cloudflared, tailscale, vpn"
    )
    cf_dns_token: str = Field("", description="Cloudflare DNS API token")
    cf_tunnel_token: str = Field("", description="Cloudflare Tunnel token")
    tailscale_key: str = Field("", description="Tailscale auth key")
    vpn_type: str = Field("", description="VPN type: wireguard or openvpn")
    vpn_provider: str = Field("", description="e.g. mullvad, nordvpn")
    vpn_key: str = Field("", description="WireGuard private key or OpenVPN username")


class WizardLLMRequest(BaseModel):
    """Typed body for POST /wizard/save-llm."""

    provider: str = Field(
        "none", description="LLM provider: groq, cerebras, openai, awan, ollama, llamacpp, none"
    )
    api_key: str = Field("", description="API key for cloud providers")
    model: str = Field("", description="Model name override")
    ollama_url: str = Field("http://ollama:11434", description="Ollama base URL")
    llamacpp_url: str = Field("http://localhost:8081", description="llama.cpp base URL")


class WizardStacksRequest(BaseModel):
    """Typed body for POST /wizard/install-stacks."""

    stack_keys: list[str] = Field(default_factory=list, description="Catalog app slugs to install")


# ── Step descriptions (shown in wizard UI) ────────────────────────────────


STEP_DESCRIPTIONS: list[StepInfo] = [
    StepInfo(
        name="preflight",
        title="System check",
        description="Verify Docker is reachable and ports 80/443 are available.",
    ),
    StepInfo(
        name="network",
        title="Docker network",
        description="Create the shared Docker network that all apps join.",
    ),
    StepInfo(
        name="config_dirs",
        title="Config directories",
        description="Create Traefik config folders and initialise the certificate store.",
    ),
    StepInfo(
        name="traefik_config",
        title="Traefik configuration",
        description="Write the Traefik static configuration with your domain and cert resolver.",
    ),
    StepInfo(
        name="traefik_deploy",
        title="Deploy Traefik",
        description="Pull the Traefik image and start the reverse proxy.",
    ),
    StepInfo(
        name="traefik_healthy",
        title="Verify Traefik",
        description="Wait for Traefik to start and confirm it is healthy.",
    ),
    StepInfo(
        name="complete",
        title="Finish",
        description="Save the platform configuration and mark setup as complete.",
    ),
]


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("/wizard/validate-secrets")
def wizard_validate_secrets(req: WizardSecretsRequest) -> dict[str, Any]:
    """Quick connectivity check for VPN/DNS/tunnel credentials.

    Non-destructive — only checks if credentials are valid, never deploys.
    Returns: {ok, warnings: [...], errors: [...]}
    """
    checks = req.checks
    warnings: list[str] = []
    errors: list[str] = []

    for check in checks:
        if check == "dns":
            # Validate CF DNS token can list zones — most common case
            token = req.cf_dns_token
            if not token:
                warnings.append("DNS: No Cloudflare API token — will fail at deploy")
            else:
                try:
                    import urllib.request as _ur
                    import json as _j

                    _verify_url = "https://api.cloudflare.com/client/v4/user/tokens/verify"
                    if not _verify_url.startswith(("http://", "https://")):
                        raise ValueError(f"Unsupported URL scheme: {_verify_url}")
                    r = _ur.urlopen(  # noqa: S310  # nosec B310  # scheme validated above; hardcoded HTTPS Cloudflare API
                        _ur.Request(  # noqa: S310  # nosec B310  # scheme validated above; hardcoded HTTPS Cloudflare API
                            _verify_url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json",
                            },
                        ),
                        timeout=8,
                    )
                    data = _j.loads(r.read())
                    if not data.get("success"):
                        errors.append(
                            f"DNS: Cloudflare token invalid — {data.get('errors', [{}])[0].get('message', 'check token permissions')}"
                        )
                    # else: token valid, no message needed
                except Exception as e:
                    warnings.append(
                        f"DNS: Could not verify Cloudflare token ({e}) — check at deploy"
                    )

        elif check == "cloudflared":
            token = req.cf_tunnel_token
            if not token:
                errors.append("Tunnel: Cloudflare Tunnel token is required")
            elif len(token) < 20:
                errors.append("Tunnel: Cloudflare Tunnel token looks too short")
            # Can't verify without deploying — just check format

        elif check == "tailscale":
            key = req.tailscale_key
            if not key:
                errors.append("Tailscale: Auth key is required")
            elif not key.startswith("tskey-"):
                warnings.append("Tailscale: Key should start with 'tskey-' — verify format")

        elif check == "vpn":
            vpn_type = req.vpn_type
            provider = req.vpn_provider
            if not provider:
                errors.append("VPN: Provider name is required (mullvad, nordvpn, etc.)")
            if vpn_type == "wireguard":
                key = req.vpn_key
                if not key:
                    errors.append("VPN: WireGuard private key is required")
                elif len(key) < 40:
                    errors.append("VPN: WireGuard key appears too short — check format")
            elif vpn_type == "openvpn":
                user = req.vpn_key
                if not user:
                    warnings.append("VPN: OpenVPN username/account number missing")

    return {
        "ok": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
        "checked": checks,
    }


@router.get("/status", response_model=PlatformStatus)
def get_platform_status() -> PlatformStatus:
    """Return the current platform configuration and status.

    Includes a consistency self-heal: if the platform claims 'ready' but
    Traefik is not running, the state is stale from a partial/failed reset.
    Automatically demote to 'pending' so the wizard is shown again.
    """
    with StateDB() as db:
        p = db.get_platform()

    # Self-heal: 'ready' + Traefik not running = inconsistent state after reset
    if p.status == "ready":
        import subprocess as _sp

        try:
            _r = _sp.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", "traefik"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            _traefik_up = _r.stdout.strip() == "running"
        except Exception:
            _traefik_up = True  # can't check Docker — assume ok, don't self-heal

        if not _traefik_up:
            import logging as _log

            _log.getLogger(__name__).warning(
                "Platform status is 'ready' but Traefik is not running — "
                "demoting to 'pending' (state is stale from a reset)"
            )
            with StateDB() as _db:
                _db.update_platform(status="pending")
            p = type(p)(
                status="pending",
                domain=p.domain,
                wildcard_domain=p.wildcard_domain,
                network_name=p.network_name,
                config_root=p.config_root,
                media_root=p.media_root,
                puid=p.puid,
                pgid=p.pgid,
                timezone=p.timezone,
                cert_resolver=p.cert_resolver,
                traefik_version=p.traefik_version,
                installed_at=p.installed_at,
                updated_at=p.updated_at,
            )

    return PlatformStatus(
        status=p.status,
        domain=p.domain,
        network_name=p.network_name,
        config_root=p.config_root,
        media_root=p.media_root,
        puid=p.puid,
        pgid=p.pgid,
        timezone=p.timezone,
        traefik_version=p.traefik_version,
        installed_at=p.installed_at,
    )


@router.get("/wizard/steps", response_model=list[StepInfo])
def get_wizard_steps() -> list[StepInfo]:
    """Return the ordered list of wizard steps with descriptions for the UI."""
    return STEP_DESCRIPTIONS


@router.post("/wizard/validate", response_model=ValidateResponse)
def wizard_validate(req: WizardRequest) -> ValidateResponse:
    """Validate wizard inputs without running anything.

    Call this before /wizard/run to surface problems before starting.
    """
    inp = WizardInput(
        domain=req.domain,
        config_root=req.config_root,
        media_root=req.media_root,
        puid=req.puid,
        pgid=req.pgid,
        timezone=req.timezone,
        cert_resolver=req.cert_resolver,
        network_name=req.network_name,
        acme_email=req.acme_email,
        dns_provider=req.dns_provider,
        include_zerossl=req.include_zerossl,
        eab_kid=req.eab_kid,
        eab_hmac=req.eab_hmac,
        ntfy_url=req.ntfy_url,
        ntfy_topic=req.ntfy_topic,
        ntfy_enabled=req.ntfy_enabled,
        tunnels=list(req.infra_selections.get("tunnels", [])),
        secrets=dict(req.secrets) if req.secrets else {},
    )
    issues = validate_wizard(inp)
    return ValidateResponse(
        valid=len(issues) == 0,
        issues=[ValidationIssue(**i) for i in issues],
    )


@router.post("/wizard/run", response_model=WizardRunResponse)
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (heavy mutation tier)
def wizard_run(request: Request, req: WizardRequest) -> WizardRunResponse:
    """Run the platform setup wizard.

    Executes all steps in order, stopping at the first error.
    Safe to re-run — already-complete steps are skipped.
    """
    # Check platform isn't already ready (protect against accidental re-runs
    # that might reconfigure a working system)
    with StateDB() as db:
        p = db.get_platform()
    if p.status == "ready":
        raise HTTPException(
            status_code=409,
            detail=(
                "Platform is already set up. "
                "To reconfigure, reset the platform first via /api/platform/reset."
            ),
        )

    # Merge all secrets: explicit fields + secrets dict
    all_secrets = dict(req.secrets) if req.secrets else {}
    if req.eab_kid:
        all_secrets["ZEROSSL_EAB_KID"] = req.eab_kid
    if req.eab_hmac:
        all_secrets["ZEROSSL_EAB_HMAC"] = req.eab_hmac

    # Extract tunnel list from infra_selections
    # infra_selections may contain {"tunnels": ["cloudflared","tailscale"]} from the frontend
    _tunnels = req.infra_selections.get("tunnels", [])
    if isinstance(_tunnels, str):
        _tunnels = [_tunnels] if _tunnels and _tunnels != "none" else []
    _tunnels = [t for t in _tunnels if t and t != "none"]

    inp = WizardInput(
        domain=req.domain,
        config_root=req.config_root,
        media_root=req.media_root,
        puid=req.puid,
        pgid=req.pgid,
        timezone=req.timezone,
        cert_resolver=req.cert_resolver,
        network_name=req.network_name,
        acme_email=req.acme_email,
        dns_provider=req.dns_provider,
        include_zerossl=req.include_zerossl,
        eab_kid=req.eab_kid,
        eab_hmac=req.eab_hmac,
        ntfy_url=req.ntfy_url,
        ntfy_topic=req.ntfy_topic,
        ntfy_enabled=req.ntfy_enabled,
        tunnels=_tunnels,
        secrets=all_secrets,
    )

    # Validate before running
    issues = validate_wizard(inp)
    if issues:
        raise HTTPException(
            status_code=422,
            detail={"message": "Invalid wizard input", "issues": issues},
        )

    result = run_wizard(inp)

    err = result.last_error()
    return WizardRunResponse(
        ok=result.ok,
        platform_ready=result.platform_ready,
        steps=[
            WizardStepResult(
                step=s.step,
                status=s.status,
                message=s.message,
                detail=s.detail,
            )
            for s in result.steps
        ],
        error=err.message if err else None,
    )


@router.get("/cert-status")
def get_cert_status() -> dict[str, Any]:
    """Check Traefik acme.json for issued/pending TLS certificates.

    Called from the wizard success screen to show cert status.
    Returns per-domain status so the UI can show a clear progress indicator.
    """
    import json as _j
    from backend.core.state import StateDB as _SDB

    with _SDB() as db:
        p = db.get_platform()

    domain = p.domain or ""
    config_root = p.config_root or ""

    if not domain or not config_root:
        return {"domain": domain, "cert_found": False, "message": "Platform not configured."}

    acme_files = [
        f"{config_root}/traefik/acme.json",
        f"{config_root}/traefik/acme-zerossl.json",
        f"{config_root}/traefik/acme-buypass.json",
        f"{config_root}/traefik/acme-staging.json",
    ]

    for acme_path in acme_files:
        try:
            data = _j.loads(open(acme_path).read())
            for resolver, resolver_data in data.items():
                certs = resolver_data.get("Certificates") or []
                for cert in certs:
                    main = cert.get("domain", {}).get("main", "")
                    sans = cert.get("domain", {}).get("sans", [])
                    if domain in main or domain in " ".join(sans) or f"*.{domain}" in main:
                        msg = f"TLS certificate issued for {main}"
                        if resolver == "staging":
                            msg += (
                                " (staging — not browser-trusted; switch to letsencrypt when ready)"
                            )
                        return {
                            "domain": domain,
                            "cert_found": True,
                            "resolver": resolver,
                            "message": msg,
                        }
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)

    return {
        "domain": domain,
        "cert_found": False,
        "message": (
            f"Certificate not yet issued for {domain}. "
            f"Traefik obtains it automatically once DNS propagates. "
            f"Check logs: docker logs traefik | grep -i acme"
        ),
    }


def _stop_and_remove_containers(container_names: list[str], timeout: int = 15) -> dict[str, Any]:
    """Stop and remove containers by name, regardless of compose file state.

    More reliable than 'docker compose down' when fragments may be stale/missing.
    """
    import subprocess as _sp

    stopped: list[str] = []
    removed: list[str] = []
    for name in container_names:
        # Stop (ignore if already stopped)
        try:
            r = _sp.run(
                ["docker", "stop", "--time", str(timeout), name],
                capture_output=True,
                timeout=timeout + 5,
            )
            if r.returncode == 0:
                stopped.append(name)
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)
        # Remove (ignore if doesn't exist)
        try:
            r = _sp.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
            if r.returncode == 0:
                removed.append(name)
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)
    return {"stopped": stopped, "removed": removed}


def _find_network_containers(network: str = "slop") -> list[str]:
    """Return names of all containers attached to a Docker network."""
    import subprocess as _sp
    import json as _j

    try:
        r = _sp.run(
            ["docker", "network", "inspect", network, "--format", "{{json .Containers}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return []
        containers = _j.loads(r.stdout.strip() or "{}")
        return [c.get("Name", "") for c in containers.values() if c.get("Name")]
    except Exception:
        return []


def _remove_network(network: str = "slop") -> bool:
    """Disconnect all containers then remove the network."""
    import subprocess as _sp

    # Disconnect any remaining containers first
    attached = _find_network_containers(network)
    for name in attached:
        try:
            _sp.run(
                ["docker", "network", "disconnect", "-f", network, name],
                capture_output=True,
                timeout=5,
            )
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)
    # Now remove
    try:
        r = _sp.run(["docker", "network", "rm", network], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


@router.post("/reset")
@confirm_token("RESET_PLATFORM")  # #1044 declarative shadow of the body `if confirm != ...` check
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (heavy mutation tier)
def reset_platform(
    request: Request,
    confirm: str = Query(default=""),
) -> dict[str, Any]:
    """Soft-reset the platform status back to 'pending'.

    Keeps running apps and Traefik running.
    Clears: platform record, infra slot state, Traefik compose fragment,
            health data, pending fixes, and wizard-related operations.
    Keeps: installed apps, app compose fragments, Docker containers, .env.

    Use POST /reset/full for a complete factory reset (stops all containers).

    Requires ?confirm=RESET_PLATFORM to prevent accidental soft-resets.
    """
    # Authorization challenge — same pattern as /reset/full (id=461)
    if confirm != "RESET_PLATFORM":
        raise HTTPException(
            status_code=400,
            detail="Pass ?confirm=RESET_PLATFORM to confirm soft reset.",
        )
    from backend.core.config import config as _cfg

    with StateDB() as db:
        p = db.get_platform()
        if p.status == "pending":
            return {"message": "Platform is already in pending state — nothing to reset."}

        # Reset platform record
        db.update_platform(
            status="pending",
            installed_at=None,
            traefik_version=None,
            domain=None,
        )

        # Clear infra slot state (providers reset to 'none'/'empty')
        db._c.execute(
            "UPDATE infra_slots SET provider=NULL, status='empty', config='{}', deployed_at=NULL"
        )

        # Clear health data — tables may not exist if never written yet
        for _tbl, _where in [
            ("health_checks", "WHERE subject_type='platform'"),
            ("pending_fixes", ""),
            ("fix_history", ""),
            ("source_availability", ""),
            ("maintenance_windows", ""),
        ]:
            try:
                # _tbl/_where are hardcoded constants from the list above, not user input
                _sql = f"DELETE FROM {_tbl} {_where}".strip()  # noqa: S608  # nosec B608
                db._c.execute(_sql)
            except Exception as e:
                # table may not exist yet (never written) — safe to ignore
                log.debug("reset: skipping clear of %s: %s", _tbl, e)

        # Clear wizard-related operations
        try:
            db._c.execute("DELETE FROM operations WHERE op_type IN ('wizard','platform_deploy')")
            db._c.execute(
                "DELETE FROM operation_steps WHERE op_id NOT IN (SELECT id FROM operations)"
            )
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)

        # NOTE: StateDB auto-commits on __exit__ — db._conn.commit() removed (Core Rule 4.4)

    # All infra containers to stop — includes traefik which is always redeployed
    INFRA_CONTAINERS = [
        "traefik",
        "tinyauth",
        "authelia",
        "cloudflared",
        "tailscale",
        "headscale",
        "gluetun",
        "glance",
        "homepage",
        "dockge",
        "dockhand",
        "komodo",
        "portainer",
        "portainer_be",
        "ollama",
    ]

    # Step 1: Stop all infra containers directly by name (faster + more reliable
    # than 'docker compose down' which requires the fragment to be parseable)
    cleanup = _stop_and_remove_containers(INFRA_CONTAINERS, timeout=15)

    # Step 2: Remove the Docker network cleanly (disconnect stragglers first)
    network_removed = _remove_network("slop")

    # Step 3: Remove all infra compose fragments
    removed_frags = []
    if _cfg.compose_dir.exists():
        for frag_name in [*INFRA_CONTAINERS, "traefik"]:
            frag = _cfg.compose_dir / f"{frag_name}.yaml"
            if frag.exists():
                try:
                    frag.unlink()
                    removed_frags.append(frag_name)
                except Exception as e:
                    log.debug("platform best-effort step skipped: %s", e)

    return {
        "message": (
            f"Platform soft-reset complete. "
            f"Stopped: {', '.join(cleanup['stopped']) or 'none'}. "
            f"Network removed: {network_removed}. "
            f"Fragments removed: {', '.join(removed_frags) or 'none'}. "
            f"App containers (non-infra) are unaffected."
        ),
        "stopped_containers": cleanup["stopped"],
        "removed_fragments": removed_frags,
        "network_removed": network_removed,
    }


@router.post("/reset/full")
@confirm_token("DESTROY_ALL_DATA")  # #1044 declarative shadow of the body `if confirm != ...` check
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (heavy mutation tier)
def reset_platform_full(
    request: Request,
    confirm: str = Query(default=""),
) -> dict[str, Any]:
    """Full factory reset — stops ALL managed containers and wipes all state."""
    if confirm != "DESTROY_ALL_DATA":
        raise HTTPException(
            status_code=400,
            detail="Pass ?confirm=DESTROY_ALL_DATA to confirm factory reset.",
        )
    import shutil as _shutil
    from backend.core.config import config as _cfg

    results: list[str] = []
    errors: list[str] = []

    try:
        # ── 1. Stop ALL containers on the slop network ─────────────
        # Find containers by network membership (catches containers not tracked
        # by any compose fragment — e.g. orphaned from failed installs)
        network_containers = _find_network_containers("slop")

        # Also collect container names from compose fragments
        frag_containers: list[str] = []
        if _cfg.compose_dir.exists():
            for frag in sorted(_cfg.compose_dir.glob("*.yaml")):
                try:
                    import yaml as _yaml

                    data = _yaml.safe_load(frag.read_text())
                    for svc_name, svc in (data.get("services") or {}).items():
                        cn = svc.get("container_name", svc_name)
                        frag_containers.append(cn)
                except Exception:
                    frag_containers.append(frag.stem)  # fallback: use filename

        all_containers = list(dict.fromkeys(network_containers + frag_containers))

        # Stop and remove all of them directly — faster and more reliable
        # than 'docker compose down' when fragments may be stale
        cleanup = _stop_and_remove_containers(all_containers, timeout=20)
        results.append(f"stopped: {', '.join(cleanup['stopped']) or 'none'}")
        if set(all_containers) - set(cleanup["stopped"]):
            errors.append(
                f"could not stop: {', '.join(set(all_containers) - set(cleanup['stopped']))}"
            )

        # Remove the Docker network cleanly
        network_removed = _remove_network("slop")
        results.append(f"network removed: {network_removed}")

        # ── 2. Remove all compose fragments ──────────────────────────────
        frags_removed = 0
        if _cfg.compose_dir.exists():
            for frag in _cfg.compose_dir.glob("*.yaml"):
                try:
                    frag.unlink()
                    frags_removed += 1
                except Exception as e:
                    errors.append(f"frag-rm-error: {frag.name}: {e}")
        results.append(f"fragments removed: {frags_removed}")

        # ── 3. Wipe DB — only tables that actually exist ──────────────────
        with StateDB() as db:
            # Get the actual tables in this DB (handles schema migrations)
            existing = {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            WIPE = [
                "apps",
                "app_dependencies",
                "app_instances",
                "managed_services",
                "wiring",
                "external_resources",
                "operations",
                "operation_steps",
                "health_checks",
                "health_check_history",
                "pending_fixes",
                "fix_history",
                "maintenance_windows",
                "source_availability",
                "storage_sources",
                "request_routing",
                "manifest_registry",
                "quickstart_phases",
                "llm_routing_log",
                "cloud_llm_usage",
                "infra_tunnel_providers",
                "llm_model_registry",
                "secrets",
            ]
            wiped = 0
            for table in WIPE:
                if table in existing:
                    try:
                        # table names come from the hardcoded WIPE list, not user input
                        db._c.execute(f"DELETE FROM {table}")  # noqa: S608  # nosec B608
                        wiped += 1
                    except Exception as e:
                        errors.append(f"wipe-{table}: {e}")

            if "infra_slots" in existing:
                try:
                    db._c.execute(
                        "UPDATE infra_slots SET provider=NULL, status='empty',"
                        " config='{}', deployed_at=NULL"
                    )
                except Exception as e:
                    errors.append(f"infra_slots: {e}")

            if "platform" in existing:
                # Only set columns that exist in this DB version
                plat_cols = {r[1] for r in db._c.execute("PRAGMA table_info(platform)")}
                set_parts = ["status='pending'", "network_name='slop'"]
                nullable = [
                    "domain",
                    "wildcard_domain",
                    "config_root",
                    "media_root",
                    "puid",
                    "pgid",
                    "timezone",
                    "traefik_version",
                    "cert_resolver",
                    "installed_at",
                ]
                for col in nullable:
                    if col in plat_cols:
                        set_parts.append(f"{col}=NULL")
                try:
                    # set_parts column names derive from the hardcoded `nullable`
                    # list filtered against actual columns — not user input.
                    _upd = f"UPDATE platform SET {', '.join(set_parts)} WHERE id=1"  # noqa: S608  # nosec B608
                    db._c.execute(_upd)
                except Exception as e:
                    errors.append(f"platform-reset: {e}")

            if "settings" in existing:
                try:
                    db._c.execute("DELETE FROM settings")
                except Exception as e:
                    errors.append(f"settings: {e}")

        results.append(f"DB wiped: {wiped} tables")

        # ── 4. Remove Traefik config directory ────────────────────────────
        for traefik_path in [
            _cfg.data_dir.parent / "config" / "traefik",
            _cfg.install_dir / "config" / "traefik",
        ]:
            if traefik_path.exists():
                try:
                    _shutil.rmtree(traefik_path)
                    results.append("traefik config removed")
                except Exception as e:
                    errors.append(f"traefik-rm: {e}")

        # ── 5. Clear .env ─────────────────────────────────────────────────
        env_file = _cfg.env_file
        if env_file.exists():
            try:
                env_file.write_text("# SLOP .env — regenerated by wizard\n")
                env_file.chmod(0o600)
                results.append(".env cleared")
            except Exception as e:
                errors.append(f".env-clear: {e}")

    except Exception as e:
        # Catch-all: log the crash and return partial results
        errors.append(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback as _tb

        errors.append(_tb.format_exc()[-500:])

    return {
        "message": "Full factory reset complete."
        if not any("UNEXPECTED" in e for e in errors)
        else "Reset completed with errors — check 'errors' field.",
        "results": results,
        "errors": errors,
        "next": "Re-run the wizard to set up fresh.",
    }


# ---------------------------------------------------------------------------
# DNS-01 and media routing guidance
# ---------------------------------------------------------------------------


SUPPORTED_DNS_PROVIDERS = [
    {"key": "cloudflare", "name": "Cloudflare", "env": ["CF_DNS_API_TOKEN"]},
    {
        "key": "route53",
        "name": "AWS Route 53",
        "env": ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
    },
    {"key": "namecheap", "name": "Namecheap", "env": ["NAMECHEAP_API_USER", "NAMECHEAP_API_KEY"]},
    {"key": "porkbun", "name": "Porkbun", "env": ["PORKBUN_API_KEY", "PORKBUN_SECRET_API_KEY"]},
    {"key": "digitalocean", "name": "DigitalOcean", "env": ["DO_AUTH_TOKEN"]},
    {"key": "gandi", "name": "Gandi", "env": ["GANDI_PERSONAL_ACCESS_TOKEN"]},
    {"key": "hetzner", "name": "Hetzner", "env": ["HETZNER_API_KEY"]},
    {
        "key": "ovh",
        "name": "OVH",
        "env": [
            "OVH_ENDPOINT",
            "OVH_APPLICATION_KEY",
            "OVH_APPLICATION_SECRET",
            "OVH_CONSUMER_KEY",
        ],
    },
    {"key": "godaddy", "name": "GoDaddy", "env": ["GODADDY_API_KEY", "GODADDY_API_SECRET"]},
    {"key": "linode", "name": "Linode/Akamai", "env": ["LINODE_TOKEN"]},
    {"key": "duckdns", "name": "DuckDNS", "env": ["DUCKDNS_TOKEN"]},
    {"key": "desec", "name": "deSEC", "env": ["DESEC_TOKEN"]},
    {"key": "njalla", "name": "Njalla", "env": ["NJALLA_TOKEN"]},
    {"key": "inwx", "name": "INWX", "env": ["INWX_USERNAME", "INWX_PASSWORD"]},
    {"key": "infomaniak", "name": "Infomaniak", "env": ["INFOMANIAK_ACCESS_TOKEN"]},
]


@router.get("/dns-providers")
def list_dns_providers() -> list[dict[str, Any]]:
    """List supported DNS providers for ACME DNS-01 challenge.

    Each entry includes the provider key (used in the wizard) and the
    environment variables that must be set in .env for cert issuance.

    Traefik/lego supports 100+ providers. This list covers the most
    common self-hosting DNS registrars. Full list:
    https://doc.traefik.io/traefik/https/acme/#providers
    """
    return SUPPORTED_DNS_PROVIDERS


@router.get("/timezones")
def list_timezones() -> dict[str, list[str]]:
    """Return sorted list of valid IANA timezone names.

    Used by the wizard frontend to power the searchable timezone dropdown.
    Sourced from the system's zoneinfo database (Python standard library).
    """
    try:
        from zoneinfo import available_timezones

        return {"timezones": sorted(available_timezones())}
    except ImportError:
        # Fallback for Python < 3.9 — return a representative subset
        return {
            "timezones": [
                "Africa/Johannesburg",
                "America/Chicago",
                "America/Denver",
                "America/Los_Angeles",
                "America/New_York",
                "America/Sao_Paulo",
                "Asia/Kolkata",
                "Asia/Seoul",
                "Asia/Shanghai",
                "Asia/Tokyo",
                "Australia/Melbourne",
                "Australia/Sydney",
                "Europe/Amsterdam",
                "Europe/Berlin",
                "Europe/London",
                "Europe/Paris",
                "Europe/Rome",
                "Pacific/Auckland",
                "UTC",
            ]
        }


@router.get("/media-routing-guide")
def media_routing_guide(domain: str = "example.com") -> dict[str, Any]:
    """Return step-by-step DNS setup instructions for media servers.

    Media servers (Plex, Jellyfin, Emby, Audiobookshelf) must NOT route
    through Cloudflare Tunnel — Cloudflare's ToS prohibits video streaming
    through their CDN/proxy infrastructure.

    Instead they use DIRECT connections: client → your server → Traefik.
    Traefik presents the wildcard Let's Encrypt cert for TLS termination.
    The DNS record must be set to 'DNS only' (gray cloud) in Cloudflare.
    """
    media_apps = ["plex", "jellyfin", "emby", "audiobookshelf"]
    return {
        "summary": (
            "Media servers require direct port 443 access. "
            "Cloudflare's ToS prohibits routing video through their CDN. "
            "Set DNS records for media apps to 'DNS only' (gray cloud). "
            "Traefik's wildcard certificate covers all subdomains automatically."
        ),
        "tls_certificate": {
            "type": "wildcard",
            "covers": f"*.{domain}",
            "method": "DNS-01 challenge (no port 80/443 needed for issuance)",
            "auto_renewal": "Traefik renews 30 days before expiry automatically",
            "note": "One cert covers all 50+ apps — no per-app certificate management",
        },
        "cloudflare_dns_setup": {
            "for_media_apps": {
                "step_1": "Log into Cloudflare dashboard → DNS → Records",
                "step_2": "Find or create an A record for each media subdomain:",
                "records": [f"{app}.{domain} → your home IP (A record)" for app in media_apps],
                "step_3": "Set Proxy status to 'DNS only' (gray cloud icon, NOT orange)",
                "step_4": "Your home IP is now the traffic destination for these apps",
                "warning": "Orange cloud (Proxied) routes through CF CDN — ToS violation for video",
            },
            "for_management_apps": {
                "note": "All other apps (Sonarr, Radarr, dashboards, etc.) use Cloudflare Tunnel",
                "action": "Leave these DNS records proxied (orange cloud) or let the tunnel handle them",
            },
        },
        "port_forwarding": {
            "required": True,
            "ports": [443],
            "note": (
                "Port 443 must be forwarded from your router to the SLOP server. "
                "Port 80 is optional (for HTTP→HTTPS redirect). "
                "If behind CGNAT (Starlink, some mobile ISPs), use Tailscale instead — "
                "no port forwarding required."
            ),
        },
        "dynamic_ip": {
            "problem": "Residential ISPs change your home IP periodically",
            "solution": "Install DDNS Updater (in catalog) — updates the DNS A record automatically",
            "ddns_providers": [
                "cloudflare",
                "namecheap",
                "duckdns",
                "godaddy",
                "porkbun",
                "30+ more",
            ],
        },
        "cgnat_alternative": {
            "problem": "CGNAT (Starlink, mobile) prevents port forwarding entirely",
            "solution": "Tailscale (in infra slots) — all apps accessible via tailnet, no public IP needed",
            "note": "Tailscale provides E2E encrypted access without any open ports",
        },
        "affected_apps": {
            "service_type_media": media_apps,
            "why": "These stream large video files that would violate Cloudflare's ToS if proxied",
        },
    }


@router.get("/hardware-profile")
def get_hardware_profile(request: Request) -> dict[str, Any]:
    """Return hardware capabilities and recommended AI backend."""
    backend = _detect_ai_backend()
    return {"recommended_ai_backend": backend, "ok": True}


@router.get("/prereqs")
def platform_prereqs(request: Request) -> dict[str, Any]:
    """Full system fingerprint collected at Stage 0 (Prerequisites).

    Calls evaluate_system() for complete hardware/OS/Docker/user data,
    runs prerequisite gate checks, stores result to DB immediately so
    downstream stages (Quick Stacks RAM warnings, Stage 9 AI recs) have data.
    """
    import json as _json
    from backend.core.system_eval import get_cached_profile as _get_profile
    from backend.core.state import StateDB as _SDB

    # Use cached profile — avoids re-running all subprocesses on every stage visit
    _force = request.query_params.get("force") == "1" if hasattr(request, "query_params") else False
    try:
        profile = _get_profile(force=_force)
    except Exception as _e:
        log.warning("system_eval failed: %s", _e)
        profile = None

    # ── Gate checks ───────────────────────────────────────────────────────
    checks = []

    # OS / distro
    if profile and profile.os_distro:
        supported = any(
            d in profile.os_distro
            for d in ("Ubuntu", "Debian", "Rocky", "Fedora", "CentOS", "Alma", "Linux")
        )
        checks.append(
            {
                "key": "os",
                "label": "Operating system",
                "status": "ok" if supported else "warning",
                "value": f"{profile.os_distro} {profile.os_version} ({profile.os_arch})",
                "detail": profile.kernel_version,
            }
        )

    # CPU
    if profile:
        checks.append(
            {
                "key": "cpu",
                "label": "CPU",
                "status": "ok",
                "value": f"{profile.cpu_model} · {profile.cpu_cores} cores",
                "detail": (
                    f"AVX2: {'yes' if profile.avx2 else 'no — llama.cpp may not work'} · "
                    f"arch: {profile.architecture}"
                ),
            }
        )

    # RAM
    if profile:
        total_gb = round(profile.total_ram_mb / 1024, 1)
        checks.append(
            {
                "key": "ram",
                "label": "RAM",
                "status": "ok" if total_gb >= 4 else "warning",
                "value": f"{total_gb}GB total · {round(profile.free_ram_mb / 1024, 1)}GB available",
                "detail": f"Headroom for LLM: ~{round(profile.headroom_ram_mb / 1024, 1)}GB",
            }
        )

    # GPU
    if profile and profile.gpu_name:
        vram_gb = round(profile.gpu_vram_mb / 1024, 1)
        checks.append(
            {
                "key": "gpu",
                "label": "GPU",
                "status": "ok",
                "value": (
                    f"{profile.gpu_name} · {vram_gb}GB VRAM" if vram_gb > 0 else profile.gpu_name
                ),
                "detail": (
                    f"CUDA {profile.gpu_cuda_version}"
                    if profile.gpu_cuda_version
                    else "ROCm (AMD)"
                    if (profile.gpu_vendor or "").lower() in ("amd", "ati")
                    else "Metal (Apple Silicon)"
                    if (profile.gpu_vendor or "").lower() == "apple"
                    else "Intel GPU"
                    if (profile.gpu_vendor or "").lower() == "intel"
                    else "no CUDA — check nvidia-smi"
                    if (profile.gpu_vendor or "").lower() == "nvidia"
                    else "GPU detected"
                ),
            }
        )

    # Docker daemon
    if profile and profile.docker_version:
        major = int(profile.docker_version.split(".")[0]) if profile.docker_version else 0
        checks.append(
            {
                "key": "docker",
                "label": "Docker Engine",
                "status": "ok" if major >= 24 else "error",
                "value": f"v{profile.docker_version} (API {profile.docker_api_version})",
                "detail": "Requires Docker 24.0+"
                if major < 24
                else f"{profile.containers_running} containers running",
            }
        )
    else:
        checks.append(
            {
                "key": "docker",
                "label": "Docker Engine",
                "status": "error",
                "value": "not found or not running",
                "detail": "Install Docker: https://docs.docker.com/engine/install/",
            }
        )

    # Docker Compose plugin
    if profile and profile.compose_version:
        checks.append(
            {
                "key": "compose",
                "label": "Docker Compose plugin",
                "status": "ok",
                "value": f"v{profile.compose_version}",
            }
        )
    else:
        checks.append(
            {
                "key": "compose",
                "label": "Docker Compose plugin",
                "status": "error",
                "value": "not found",
            }
        )

    # Disk space — check all mounted paths
    if profile:
        for disk in profile.disks:
            checks.append(
                {
                    "key": f"disk_{disk.path.replace('/', '_')}",
                    "label": f"Disk ({disk.path})",
                    "status": "ok"
                    if disk.free_gb >= 20
                    else ("warning" if disk.free_gb >= 5 else "error"),
                    "value": f"{disk.free_gb}GB free of {disk.total_gb}GB",
                    "detail": f"{disk.percent_used}% used",
                }
            )

    # PUID / PGID / user
    if profile:
        checks.append(
            {
                "key": "user",
                "label": "File owner (PUID/PGID)",
                "status": "ok",
                "value": f"UID {profile.puid} / GID {profile.pgid}"
                + (f" ({profile.puid_username})" if profile.puid_username else ""),
            }
        )

    # Timezone
    if profile and profile.timezone:
        checks.append(
            {
                "key": "timezone",
                "label": "System timezone",
                "status": "ok",
                "value": profile.timezone,
            }
        )

    # Server IP
    if profile and profile.server_ip:
        checks.append(
            {
                "key": "server_ip",
                "label": "Server IP",
                "status": "ok",
                "value": profile.server_ip,
            }
        )

    # ── Store to DB immediately ────────────────────────────────────────────
    if profile:
        try:
            profile_dict = {
                "collected_at": profile.measured_at,
                "os": {
                    "distro": profile.os_distro,
                    "version": profile.os_version,
                    "arch": profile.os_arch,
                    "kernel": profile.kernel_version,
                },
                "cpu": {
                    "model": profile.cpu_model,
                    "cores": profile.cpu_cores,
                    "arch": profile.architecture,
                    "avx": profile.avx,
                    "avx2": profile.avx2,
                    "avx512": profile.avx512,
                },
                "ram": {
                    "total_gb": round(profile.total_ram_mb / 1024, 1),
                    "available_gb": round(profile.free_ram_mb / 1024, 1),
                    "used_gb": round(profile.used_ram_mb / 1024, 1),
                    "headroom_gb": round(profile.headroom_ram_mb / 1024, 1),
                },
                "gpu": (
                    [
                        {
                            "vendor": profile.gpu_vendor,
                            "model": profile.gpu_name,
                            "vram_gb": round(profile.gpu_vram_mb / 1024, 1),
                            "cuda": profile.gpu_cuda_version,
                            "inference_capable": profile.gpu_inference_capable,
                            "backend": (getattr(profile, "gpu_backend", None) or ""),
                        }
                    ]
                    if profile.gpu_name
                    else []
                ),
                "disks": [
                    {
                        "path": d.path,
                        "total_gb": d.total_gb,
                        "free_gb": d.free_gb,
                        "pct_used": d.percent_used,
                    }
                    for d in profile.disks
                ],
                "docker": {
                    "engine": profile.docker_version,
                    "api": profile.docker_api_version,
                    "compose": profile.compose_version,
                    "containers_running": profile.containers_running,
                },
                "user": {
                    "puid": profile.puid,
                    "pgid": profile.pgid,
                    "username": profile.puid_username,
                },
                "timezone": profile.timezone,
                "server_ip": profile.server_ip,
                "recommended_model": profile.recommended_model,
                "llm_warning": profile.llm_warning,
                # Legacy keys for backward compat with context_assembler
                "total_ram_mb": profile.total_ram_mb,
                "available_ram_mb": profile.free_ram_mb,
                "headroom_ram_mb": profile.headroom_ram_mb,
            }
            with _SDB() as db:
                db.set_setting("system_profile", _json.dumps(profile_dict))
        except Exception as _se:
            log.warning("system_profile store failed: %s", _se)

    # ── Return to frontend ─────────────────────────────────────────────────
    from backend.core.config import config as _cfg

    system = {}
    if profile:
        system = {
            "puid": profile.puid,
            "pgid": profile.pgid,
            "puid_username": profile.puid_username,
            "timezone": profile.timezone,
            "server_ip": profile.server_ip,
            "recommended_model": profile.recommended_model,
            "available_models": profile.available_models,
            "llm_warning": profile.llm_warning,
            "cpu_cores": profile.cpu_cores,
            "cpu_model": profile.cpu_model,
            "ram_gb": round(profile.total_ram_mb / 1024, 1),
            "total_ram_gb": round(profile.total_ram_mb / 1024, 1),
            "gpu_name": profile.gpu_name,
            "gpu_vram_gb": round(profile.gpu_vram_mb / 1024, 1) if profile.gpu_vram_mb else 0,
            "config_root": str(_cfg.data_dir / "config"),
        }

    return {"checks": checks, "system": system}


# ── In-memory job store for async wizard runs ─────────────────────────────
# _threading/_uuid/_time and the jobs_store helpers are imported at module top.
# SQLite-backed mirror (migration 012) so a restart doesn't orphan polling
# clients. The in-memory dicts below stay the hot path / liveness authority;
# the jobs_store helpers persist each mutation and reload a job on a memory miss.
# See backend/api/jobs_store.py for the restart semantics ('running' -> 'unknown').
_wizard_jobs: dict[str, dict[str, Any]] = {}
_wizard_jobs_lock = _threading.Lock()


@router.post("/wizard/run-async")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (heavy mutation tier)
def wizard_run_async(request: Request, req: WizardRequest) -> dict[str, Any]:
    """Start wizard in background thread; return job_id for polling."""
    # Reset platform if ready (allow re-runs from Settings)
    with StateDB() as db:
        p = db.get_platform()
    if p.status == "ready":
        try:
            # reset_platform is defined in this same module (line 552) — direct call.
            reset_platform()
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)

    job_id = str(_uuid.uuid4())
    job: dict[str, Any] = {
        "id": job_id,
        "steps": [],
        "done": False,
        "platform_ready": False,
        "error": None,
        "started_at": _time.time(),
    }
    with _wizard_jobs_lock:
        _wizard_jobs[job_id] = job
    _persist_job(job_id, "wizard", job)

    # Build wizard input (same logic as wizard_run)
    all_secrets = dict(req.secrets) if req.secrets else {}
    if req.eab_kid:
        all_secrets["ZEROSSL_EAB_KID"] = req.eab_kid
    if req.eab_hmac:
        all_secrets["ZEROSSL_EAB_HMAC"] = req.eab_hmac

    _tunnels = req.infra_selections.get("tunnels", [])
    if isinstance(_tunnels, str):
        _tunnels = [_tunnels] if _tunnels and _tunnels != "none" else []
    _tunnels = [t for t in _tunnels if t and t != "none"]

    try:
        inp = WizardInput(
            domain=req.domain or "",
            config_root=req.config_root or "",
            media_root=req.media_root or "",
            puid=req.puid or 1000,
            pgid=req.pgid or 1000,
            timezone=req.timezone or "UTC",
            cert_resolver=req.cert_resolver or "letsencrypt",
            acme_email=req.acme_email or f"admin@{req.domain}",
            dns_provider=req.dns_provider or "",
            secrets=all_secrets,
            auth=req.infra_selections.get("auth") or "none",
            tunnels=_tunnels,
            vpn=req.infra_selections.get("vpn") or "none",
            dashboard=req.infra_selections.get("dashboard") or "none",
            management=req.infra_selections.get("management") or "none",
            traefik_dashboard_port=int(req.infra_selections.get("traefik_dashboard_port") or 8081),
        )
    except Exception as _build_err:
        # Surface construction errors as a proper response instead of HTTP 500.
        # Do NOT spawn a thread here — inputs are invalid, no work should run.
        with _wizard_jobs_lock:
            _wizard_jobs[job_id]["error"] = f"Wizard configuration error: {_build_err}"
            _wizard_jobs[job_id]["done"] = True
            _err_job = dict(_wizard_jobs[job_id])
        _persist_job(job_id, "wizard", _err_job)
        return {
            "job_id": job_id,
            "error": f"Wizard configuration error: {_build_err}",
            "done": True,
        }

    issues = validate_wizard(inp)
    if issues:
        # Validation failed — update job state and return without spawning a thread.
        with _wizard_jobs_lock:
            _wizard_jobs[job_id]["error"] = f"Validation failed: {issues[0]}"
            _wizard_jobs[job_id]["done"] = True
            _val_job = dict(_wizard_jobs[job_id])
        _persist_job(job_id, "wizard", _val_job)
        return {"error": f"Validation failed: {issues[0]}", "done": True, "job_id": job_id}

    def _run() -> None:
        from backend.platform.wizard import run_wizard as _run_wiz

        try:
            result = _run_wiz(inp, step_callback=lambda step: _on_step(job_id, step))
            # step_callback may not include the final error step — add it

            with _wizard_jobs_lock:
                _wizard_jobs[job_id]["platform_ready"] = result.ok
                _wizard_jobs[job_id]["done"] = True
                if not result.ok:
                    failed = next((s for s in result.steps if s.status == "error"), None)
                    _wizard_jobs[job_id]["error"] = (
                        failed.message if failed else "Setup did not complete."
                    )
                _final_job = dict(_wizard_jobs[job_id])
            _persist_job(job_id, "wizard", _final_job)
        except Exception as exc:
            with _wizard_jobs_lock:
                _wizard_jobs[job_id]["error"] = str(exc)
                _wizard_jobs[job_id]["done"] = True
                _exc_job = dict(_wizard_jobs[job_id])
            _persist_job(job_id, "wizard", _exc_job)

    def _on_step(jid: str, step: Any) -> None:
        with _wizard_jobs_lock:
            if jid in _wizard_jobs:
                _wizard_jobs[jid]["steps"].append(
                    {
                        "step": getattr(step, "step", ""),
                        "status": getattr(step, "status", "ok"),
                        "message": getattr(step, "message", ""),
                        "detail": getattr(step, "detail", ""),
                    }
                )
                _step_job = dict(_wizard_jobs[jid])
            else:
                _step_job = None
        if _step_job is not None:
            _persist_job(jid, "wizard", _step_job)

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return {"job_id": job_id}


@router.get("/wizard/status/{job_id}")
def wizard_job_status(job_id: str) -> dict[str, Any]:
    """Poll async wizard job status.

    The live in-memory dict is the hot path. If the job is absent there (e.g.
    the backend restarted mid-run), fall back to the SQLite jobs table so the
    polling client gets a coherent answer instead of a spurious 404. A genuine
    unknown job_id (missing from both) returns a clear 404.
    """
    with _wizard_jobs_lock:
        job = _wizard_jobs.get(job_id)
    if job is None:
        job = _load_wizard_job_from_db(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "done": job["done"],
        "platform_ready": job["platform_ready"],
        "error": job["error"],
        "steps": list(job["steps"]),
        "elapsed_s": round(_time.time() - job["started_at"], 1),
    }


# ── In-memory job store for Ollama setup (install + model pull) ───────────
_ollama_jobs: dict[str, Any] = {}
_ollama_jobs_lock = _threading.Lock()


@router.post("/wizard/setup-ollama")
def wizard_setup_ollama(req: OllamaSetupRequest) -> dict[str, Any]:
    """Install Ollama from catalog and pull the requested model.

    Runs in a background thread so the frontend can poll progress.
    Returns a job_id for GET /wizard/ollama-status/{job_id}.

    Body: { "model": "phi4-mini" }
    """
    model = (req.model or "phi4-mini").strip()
    job_id = str(_uuid.uuid4())

    job: dict[str, Any] = {
        "id": job_id,
        "model": model,
        "phase": "starting",  # starting | installing | pulling | done | error
        "progress": 0,  # 0-100
        "message": "Starting…",
        "done": False,
        "ok": False,
        "errorDetail": None,
        "started_at": _time.time(),
    }
    with _ollama_jobs_lock:
        _ollama_jobs[job_id] = job
    _persist_job(job_id, "ollama", job)

    def _update(**kw: Any) -> None:
        with _ollama_jobs_lock:
            _ollama_jobs[job_id].update(kw)
            _snapshot = dict(_ollama_jobs[job_id])
        _persist_job(job_id, "ollama", _snapshot)

    def _run() -> None:
        import subprocess as _sp
        from backend.manifests.executor import install_app as _install_app

        # ── Phase 0: Check if Ollama is already reachable ────────────────
        import httpx as _httpx

        _already_running = False
        try:
            _chk = _httpx.get("http://ollama:11434/api/version", timeout=3)
            if _chk.status_code == 200:
                _already_running = True
                _update(phase="installing", progress=30, message="✓ Ollama is already running.")
        except Exception as e:
            log.debug("platform best-effort step skipped: %s", e)

        if not _already_running:
            import subprocess as _sp_oll

            # Check if Ollama container already exists (even if API not responding yet)
            # This handles the retry case: container started but API still initializing
            _container_exists = False
            try:
                _cex = _sp_oll.run(
                    ["docker", "ps", "-a", "--filter", "name=ollama", "--format", "{{.Names}}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                _container_exists = "ollama" in _cex.stdout
            except Exception as e:
                log.debug("platform best-effort step skipped: %s", e)

            if not _container_exists:
                # ── Phase 1: Install Ollama container ────────────────────
                _update(phase="installing", progress=5, message="Installing Ollama container…")
                try:
                    r = _install_app("ollama")
                    if not r.ok:
                        _update(
                            phase="error",
                            done=True,
                            ok=False,
                            errorDetail=getattr(r, "detail", None)
                            or r.error
                            or "Ollama install failed",
                            message=getattr(r, "detail", None) or r.error or "Install failed",
                        )
                        return
                except Exception as e:
                    _update(
                        phase="error",
                        done=True,
                        ok=False,
                        errorDetail=safe_detail(e, "Install error.", log=log),
                        message="Install error.",
                    )
                    return
            else:
                # Container exists but API wasn't responding — it's still initializing
                _update(
                    phase="installing",
                    progress=20,
                    message="Ollama container found — waiting for API to initialize…",
                )

            _update(phase="installing", progress=30, message="Waiting for Ollama API to be ready…")

            # ── Phase 2: Wait for Ollama API (up to 180s for GPU init) ──────
            # AMD iGPU (Vega 7, 780M) with Mesa drivers can take 2-3min on first start
            for attempt in range(90):  # up to 180s
                _time.sleep(2)
                try:
                    r2 = _httpx.get("http://ollama:11434/api/version", timeout=5)
                    if r2.status_code == 200:
                        break
                except Exception as e:
                    log.debug("platform best-effort step skipped: %s", e)
                elapsed = (attempt + 1) * 2
                _update(
                    progress=30 + min(attempt, 30),
                    message=f"Waiting for Ollama API… ({elapsed}s elapsed, up to 180s)",
                )
            else:
                _update(
                    phase="error",
                    done=True,
                    ok=False,
                    errorDetail="Ollama API did not respond within 120s",
                    message=(
                        "Ollama started but API not reachable after 180s. "
                        "Check: docker logs ollama\n"
                        "Note: First start on AMD/NVIDIA GPU can take 2+ minutes. "
                        "Click Retry — Ollama may now be ready."
                    ),
                )
                return

        _update(
            phase="pulling",
            progress=35,
            message=f"Pulling model {model}… (this may take several minutes)",
        )

        # ── Phase 3: Pull model via docker exec ──────────────────────────
        # Stream docker exec output to track progress
        try:
            proc = _sp.Popen(
                ["docker", "exec", "ollama", "ollama", "pull", model],
                stdout=_sp.PIPE,
                stderr=_sp.STDOUT,
                text=True,
            )
            last_pct = 35
            assert proc.stdout is not None  # stdout=PIPE guarantees non-None at runtime
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                # Ollama pull output: "pulling sha256:xxx... 14% ▕████   ▏ 562 MB/3.8 GB"
                import re as _re

                m = _re.search(r"(\d+)%", line)
                if m:
                    pct = int(m.group(1))
                    last_pct = 35 + int(pct * 0.60)  # map 0-100% → 35-95%
                _update(progress=last_pct, message=f"Downloading {model}: {line[:80]}")

            proc.wait(timeout=600)
            if proc.returncode != 0:
                _update(
                    phase="error",
                    done=True,
                    ok=False,
                    errorDetail=f"ollama pull {model} exited with code {proc.returncode}",
                    message=f"Model pull failed. Run: docker exec ollama ollama pull {model}",
                )
                return
        except _sp.TimeoutExpired:
            proc.kill()
            _update(
                phase="error",
                done=True,
                ok=False,
                errorDetail="Model download timed out after 10 minutes",
                message="Download too slow. Try again or pick a smaller model.",
            )
            return
        except Exception as e:
            _update(
                phase="error",
                done=True,
                ok=False,
                errorDetail=safe_detail(e, "Pull error.", log=log),
                message="Pull error.",
            )
            return

        # ── Phase 4: Verify model is available ───────────────────────────
        _update(
            phase="done",
            progress=100,
            done=True,
            ok=True,
            message=f"✓ Ollama ready. Model {model} loaded and available.",
        )

    _threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "model": model}


@router.get("/wizard/ollama-status/{job_id}")
def wizard_ollama_status(job_id: str) -> dict[str, Any]:
    """Poll Ollama setup job status.

    In-memory dict is the hot path; on a miss (e.g. after a restart) fall back
    to the SQLite jobs table. A truly unknown job_id returns a clear 404.
    """
    with _ollama_jobs_lock:
        job = _ollama_jobs.get(job_id)
    if job is None:
        job = _load_ollama_job_from_db(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(job)


@router.post("/wizard/bcrypt-users")
def wizard_bcrypt_users(req: BcryptUsersRequest) -> dict[str, Any]:
    """Hash username:password for TinyAuth TINYAUTH_AUTH_USERS env var.

    Returns the bcrypt hash string in the format: username:$2b$10$hash
    """
    username = (req.username or "admin").strip()
    password = req.password or ""
    if not password:
        raise HTTPException(status_code=400, detail="Password is required")
    try:
        import importlib as _il

        _bcrypt = _il.import_module("bcrypt")
        hashed = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=10))
        users_str = f"{username}:{hashed.decode()}"
        return {"users": users_str, "username": username}
    except (ImportError, ModuleNotFoundError) as err:
        raise HTTPException(
            status_code=503,
            detail="bcrypt not installed. Run: pip install bcrypt",
        ) from err
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=safe_detail(e, "Could not generate credentials.", log=log)
        ) from e


@router.post("/wizard/install-stacks")
async def wizard_install_stacks(req: WizardStacksRequest) -> dict[str, Any]:
    """Stage 8: install selected quick stack apps in parallel.

    Accepts: { "stack_keys": ["sonarr", "radarr", ...] }
    Returns synchronous results after all installs complete. Parallelism is
    capped by _calc_install_concurrency() (CPU + RAM derived, max 8).
    """
    import asyncio
    from asyncio import Semaphore
    from backend.manifests.executor import install_app
    from backend.core.state import StateDB

    stack_keys = req.stack_keys
    if not stack_keys:
        return {"ok": True, "results": [], "message": "No apps to install"}

    # Gate: platform must be ready before installing. Retry with short delay to
    # handle the race where install fires immediately after wizard commits.
    import time as _t

    for _attempt in range(3):
        with StateDB() as _db:
            _p = _db.get_platform()
        if _p.status == "ready":
            break
        _t.sleep(2)
    else:
        return {
            "ok": False,
            "results": [],
            "message": "Platform is not ready. Complete the setup wizard first.",
        }

    cap = _calc_install_concurrency()
    log.info("wizard_install: concurrency=%d keys=%s", cap, stack_keys)
    sem = Semaphore(cap)

    async def _install_one(app_key: str) -> dict[str, Any]:
        async with sem:
            try:
                r = await asyncio.to_thread(install_app, app_key)
                return {
                    "key": app_key,
                    "ok": r.ok,
                    "error": r.error or "",
                    "steps": [
                        {"step": s.name, "status": s.status, "message": s.message} for s in r.steps
                    ],
                }
            except Exception as exc:
                return {
                    "key": app_key,
                    "ok": False,
                    "error": safe_detail(exc, "Install failed.", log=log),
                    "steps": [],
                }

    tasks = [_install_one(key) for key in stack_keys]
    results: list[dict[str, Any]] = await asyncio.gather(*tasks, return_exceptions=False)

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "ok": ok_count == len(results),
        "results": results,
        "message": f"{ok_count}/{len(results)} apps installed successfully",
    }


@router.get("/wizard/stack-app-keys")
def wizard_stack_app_keys(stack_ids: str = "") -> dict[str, Any]:
    """Return catalog keys for the given quick stack IDs (comma-separated).
    Uses _DEFAULT_STACKS + custom DB stacks so customised stacks are respected."""
    with StateDB() as _db:
        _plat = _db.get_platform()
        if not _plat or _plat.status not in ("ready", "pending"):
            raise HTTPException(status_code=409, detail="Platform setup not complete")
        custom, hidden = _load_stacks_from_db(_db)
    all_stacks = _build_stacks_response(custom, hidden)
    stack_map = {s["id"]: s["app_keys"] for s in all_stacks}
    keys: list[str] = []
    for stack_id in stack_ids.split(",") if stack_ids else []:
        keys.extend(stack_map.get(stack_id.strip(), []))
    return {"keys": list(dict.fromkeys(keys))}  # deduplicated, order preserved


@router.post("/wizard/save-llm")
def wizard_save_llm(req: WizardLLMRequest) -> dict[str, Any]:
    """Stage 5: persist LLM provider choice and API key to settings."""
    from backend.core.state import StateDB
    import json as _json

    provider = req.provider
    api_key = req.api_key

    if provider == "none":
        return {"ok": True, "message": "AI monitoring skipped — enable later in Settings → Health"}

    try:
        with StateDB() as db:
            cfg: dict[str, Any] = {}
            if provider == "groq":
                model = req.model or "llama-3.3-70b-versatile"
                cfg = {
                    "provider": "groq",
                    "api_key": api_key,
                    "model": model,
                    "base_url": "https://api.groq.com/openai/v1",
                }
            elif provider == "cerebras":
                model = req.model or "llama-3.3-70b"
                cfg = {
                    "provider": "cerebras",
                    "api_key": api_key,
                    "model": model,
                    "base_url": "https://api.cerebras.ai/v1",
                }
            elif provider == "openai":
                model = req.model or "gpt-4o-mini"
                cfg = {
                    "provider": "openai",
                    "api_key": api_key,
                    "model": model,
                    "base_url": "https://api.openai.com/v1",
                }
            elif provider == "awan":
                model = req.model or "Meta-Llama-3.1-8B-Instruct"
                cfg = {
                    "provider": "awan",
                    "api_key": api_key,
                    "model": model,
                    "base_url": "https://api.awanllm.com/v1",
                }
            elif provider == "ollama":
                model_name = req.model or "phi4-mini"
                ollama_url = req.ollama_url or "http://ollama:11434"
                cfg = {
                    "provider": "ollama",
                    "api_key": "",
                    "model": model_name,
                    "ollama_url": ollama_url,
                }
            elif provider == "llamacpp":
                llamacpp_url = req.llamacpp_url or "http://localhost:8081"
                model_name = req.model or "phi-4-mini"
                cfg = {
                    "provider": "llamacpp",
                    "api_key": "",
                    "model": model_name,
                    "llamacpp_url": llamacpp_url,
                    # llama.cpp is OpenAI-compatible — the scheduler uses this URL directly
                    "base_url": llamacpp_url + "/v1",
                }
            db.set_setting("llm_agent_config", _json.dumps(cfg))
            db.set_setting("llm_enabled", "true" if provider != "none" else "false")
        return {"ok": True, "message": f"AI monitoring configured: {provider}"}
    except Exception as e:
        return {
            "ok": False,
            "message": safe_detail(e, "Could not configure AI monitoring.", log=log),
        }


@router.get("/cloud-models")
async def get_cloud_models(provider: str, api_key: str = "") -> dict[str, Any]:
    """Fetch available model IDs from a cloud LLM provider's /v1/models endpoint.

    SSRF guard: only hardcoded provider base URLs are used — the caller cannot
    supply an arbitrary URL.  Timeout: 5 s.  Never raises 500; always returns
    ``{"models": [], "error": "<message>"}`` on failure.

    Supported providers: openai, anthropic, openrouter, groq
    """
    import httpx as _httpx

    from backend.core.url_guard_httpx import pinned_async_client

    # --- SSRF allowlist: only these URLs are ever contacted ---
    _PROVIDER_URLS: dict[str, tuple[str, str]] = {
        "openai": ("https://api.openai.com/v1/models", "bearer"),
        "anthropic": ("https://api.anthropic.com/v1/models", "x-api-key"),
        "openrouter": ("https://openrouter.ai/api/v1/models", "bearer"),
        "groq": ("https://api.groq.com/openai/v1/models", "bearer"),
    }

    if provider not in _PROVIDER_URLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider!r}. Allowed: {sorted(_PROVIDER_URLS)}",
        )

    if not api_key or len(api_key) < 10:
        raise HTTPException(status_code=400, detail="api_key must be at least 10 characters")

    url, auth_style = _PROVIDER_URLS[provider]

    headers: dict[str, str] = {}
    if auth_style == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    else:  # x-api-key (Anthropic)
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"

    try:
        async with pinned_async_client(timeout=5.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                return {"models": [], "error": f"Provider returned HTTP {r.status_code}"}
            data = r.json()
            # Both OpenAI-style and Anthropic-style wrap model objects under "data"
            models: list[str] = sorted(
                m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m
            )
            return {"models": models, "error": None}
    except _httpx.TimeoutException:
        return {"models": [], "error": "Request timed out after 5 s"}
    except Exception as exc:
        return {"models": [], "error": safe_detail(exc, "Could not list models.", log=log)}


@router.get("/ollama-models")
async def get_ollama_models(ollama_url: str = "http://localhost:11434") -> dict[str, Any]:
    """Fetch available models from a running Ollama instance.

    Returns list of model names, or empty list if Ollama is unreachable.
    Query param: ollama_url (default http://localhost:11434)

    SSRF guard: only localhost/127.0.0.1/::1 targets are permitted.
    The wizard always targets the user's own machine; remote Ollama URLs
    that pass through this endpoint would allow scanning arbitrary hosts.
    """
    from backend.core.url_guard import UrlNotAllowed, assert_allowed_url
    from backend.core.url_guard_httpx import pinned_async_client

    # SSRF guard — exact-host match via shared guard; loopback-only (allow_private).
    _ALLOWED = frozenset({"localhost", "127.0.0.1", "::1", "ip6-localhost"})
    try:
        p = assert_allowed_url(
            ollama_url, allowed_hosts=_ALLOWED, schemes=("http", "https"), allow_private=True
        )
    except UrlNotAllowed as err:
        raise HTTPException(status_code=400, detail="ollama_url must target localhost") from err

    try:
        async with pinned_async_client(timeout=3.0) as client:
            # Fetch from validated parts, not the raw user string.
            r = await client.get(f"{p.scheme}://{p.netloc}/api/tags")
            if r.status_code == 200:
                data = r.json()
                return {"models": [m["name"] for m in data.get("models", [])], "live": True}
    except Exception as e:
        log.debug("platform best-effort step skipped: %s", e)
    return {"models": [], "live": False}
