"""backend/manifests/loader.py

Loads and validates app manifests from YAML files.

A manifest is the single source of truth for everything SLOP
knows about an app: how to deploy it, wire it, health-check it,
and remove it. This module turns raw YAML into validated AppManifest
objects that the rest of the system uses.

Usage:
    from backend.manifests.loader import load_manifest, load_all_manifests

    manifest = load_manifest("sonarr")          # loads catalog/apps/sonarr.yaml
    all_apps = load_all_manifests()             # loads all catalog/apps/*.yaml
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.core.config import config
from backend.core.logging import get_logger

# The leaf manifest-schema dataclasses + VALID_* validation vocab live in a sibling module to keep
# this file under the production_code cap (#1302). Imported back + re-exported (redundant-alias
# idiom) so existing callers — AppManifest's annotations, loader_parsers' construction imports, and
# the tests importing VolumeDef/DependencyDef from loader — keep resolving unchanged.
from backend.manifests.loader_types import (
    VALID_CATEGORIES as VALID_CATEGORIES,
    VALID_CHECK_TYPES as VALID_CHECK_TYPES,
    VALID_HEAL_ACTIONS as VALID_HEAL_ACTIONS,
    VALID_STEP_TYPES as VALID_STEP_TYPES,
    VALID_WIRE_DIRECTIONS as VALID_WIRE_DIRECTIONS,
    DependencyDef as DependencyDef,
    GpuDef as GpuDef,
    HealthCheckDef as HealthCheckDef,
    InstallPromptDef as InstallPromptDef,
    PortDef as PortDef,
    PostDeployStep as PostDeployStep,
    SelfHealDef as SelfHealDef,
    VolumeDef as VolumeDef,
    WireDef as WireDef,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Manifest validation errors
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """A manifest file has a structural or validation problem."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path.name}: {message}")


@dataclass
class AppManifest:
    # ── Identity ──────────────────────────────────────────────────────────
    key: str
    display_name: str
    description: str
    category: str
    tier: int
    icon: str
    version: str
    image: str
    image_tag: str
    linuxserver: bool

    # ── Ports ─────────────────────────────────────────────────────────────
    web_port: int | None
    extra_ports: list[PortDef] = field(default_factory=list)

    # ── Storage ───────────────────────────────────────────────────────────
    config_volume: str = "/config"  # container path
    media_volume: str | None = None  # container path, None = not mounted
    custom_volumes: list[VolumeDef] = field(default_factory=list)

    # ── Backup ────────────────────────────────────────────────────────────
    # backup_supported opts an app into the recoverability backup machinery:
    # `ms-backup` can tar its config volume, the recovery probes track its
    # backup freshness, and the agent warns before mutating actions when no
    # recent backup exists. The backup *directory* is NOT declared here — it is
    # host-path dependent and resolved at runtime under the platform config_root
    # (see backend.agent.backup.app_backup_dir). backup_dir stays an optional
    # field so a probe/test can inject an explicit absolute path when needed.
    backup_supported: bool = False
    backup_dir: str = ""  # runtime-resolved; empty = derive from config_root
    # Per-app restore-verify sentinel (docs/BACKUP-PRODUCT-868-DESIGN.md §4). Selects the
    # per-class invariant `ms-backup --verify` applies after restoring this app's artifact to
    # scratch — the plug-point of the `backend.platform.backup_ops.Invariant` seam:
    #   ""            → generic invariant (tar extracts + ≥1 real file restored); the default.
    #   "sqlite:<rel>"→ the restored file at <rel> opens + passes PRAGMA integrity_check
    #                   (e.g. "sqlite:app.db" for an app whose state is a SQLite DB).
    #   "path:<rel>"  → the restored backup must contain <rel> (a declared sentinel file).
    # An unrecognised value falls back to the generic invariant (never a silent skip of verify).
    backup_verify: str = ""
    # backup_offhost opts an app into REQUIRING an off-host copy (design §13 / #1283): the recovery
    # probe then REFUSES VERIFIED while the latest restore-verify scope is only local-plaintext (a
    # local-only verify never proves the off-host copy decrypts). Default false = local-only.
    backup_offhost: bool = False

    # ── Environment ───────────────────────────────────────────────────────
    env: dict[str, str] = field(default_factory=dict)

    # ── Dependencies ──────────────────────────────────────────────────────
    dependencies: DependencyDef = field(default_factory=DependencyDef)

    # ── Traefik ───────────────────────────────────────────────────────────
    traefik_enabled: bool = True
    service_type: str = "management"  # management | media | internal
    traefik_subdomain: str = ""  # defaults to key if empty
    traefik_headers: dict[str, str] = field(default_factory=dict)

    # ── Wiring ────────────────────────────────────────────────────────────
    wiring: list[WireDef] = field(default_factory=list)

    # ── Post-deploy ───────────────────────────────────────────────────────
    post_deploy: list[PostDeployStep] = field(default_factory=list)

    # ── Health ────────────────────────────────────────────────────────────
    health_checks: list[HealthCheckDef] = field(default_factory=list)
    self_heal: list[SelfHealDef] = field(default_factory=list)

    # ── GPU ───────────────────────────────────────────────────────────────
    gpu: GpuDef | None = None

    # ── Hardware requirements (non-GPU) ────────────────────────────────────
    # Free-text note surfaced in the install modal when hardware dependencies
    # exist that can't be detected automatically (e.g. SMART disk, USB drives).
    hardware_note: str | None = None

    # ── Companion services (app-specific, not shared) ────────────────────
    # e.g. Karakeep needs Meilisearch + Chrome — deployed and removed with the app
    companions: list[dict[str, Any]] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    # Per-app config schema — drives the config form in app detail view
    # Each entry: {key, label, type, required, placeholder, help, secret, options}
    dashboard_icon: str = ""  # override for walkxcode icon name
    start_grace_s: int = (
        0  # seconds after container start to skip health checks (0 = use default 120s)
    )
    install_wait_s: int | None = (
        None  # patience for wait_healthy/api_ready post_deploy steps (falls back to start_grace_s)
    )
    health_grace_s: int | None = (
        None  # how long health scheduler suppresses failures for a newly-started app (falls back to start_grace_s)
    )
    pull_timeout_s: int = 600  # timeout for image pull step (seconds)
    config_schema: list[dict[str, Any]] = field(default_factory=list)
    post_install: list[str] = field(default_factory=list)  # guidance shown after install
    # Default config values (pre-fills config form on first open)
    config_defaults: dict[str, Any] = field(default_factory=dict)  # must be installed first
    recommends: list[str] = field(default_factory=list)  # optional but beneficial

    # ── FUSE / privileged requirements ───────────────────────────────────────
    # For debrid/rclone containers that need SYS_ADMIN and /dev/fuse
    capabilities: list[str] = field(default_factory=list)  # e.g. ["SYS_ADMIN"]
    security_opt: list[str] = field(default_factory=list)  # e.g. ["apparmor:unconfined"]
    devices: list[str] = field(default_factory=list)  # e.g. ["/dev/fuse:/dev/fuse:rwm"]

    # ── Extra container config (cap_add, devices, etc.) ─────────────────
    extra_config: dict[str, Any] = field(default_factory=dict)

    # ── Auto-generated install secrets ───────────────────────────────────
    # Each entry: {key: str, length: int} — generated at install time and
    # written to .env if not already present. Used for app-specific secrets
    # (session tokens, signing keys) that must exist but need no user input.
    auto_secrets: list[dict[str, Any]] = field(default_factory=list)

    # ── TLS certificate path ──────────────────────────────────────────────
    # Optional path to a PEM certificate file (or Traefik acme.json) that the
    # cert-expiry probe monitors.  Supports the ``{config_root}`` template token,
    # which the recovery-audit reconciler resolves at runtime from the platform
    # config.  Empty string means "no cert probe for this app."
    #
    # Examples:
    #   tls_cert_path: "{config_root}/traefik/acme.json"
    #   tls_cert_path: "{config_root}/myapp/certs/server.pem"
    tls_cert_path: str = ""

    # Seed config files (F6b): {dest, content} written to config_path pre-deploy.
    seed_config: list[dict[str, Any]] = field(default_factory=list)

    # ── Install prompts (id=816) ──────────────────────────────────────────────
    # Prompts surfaced to the user before install — e.g. to collect a custom
    # volume path. Each entry corresponds to a "<prompt:{key}>" sentinel in
    # a custom volume host_path.  The frontend wizard renders these as form
    # fields; the backend substitutes them in the compose fragment.
    install_prompts: list[Any] = field(default_factory=list)

    # ── Links / tags ──────────────────────────────────────────────────────
    links: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    # ── Internal ──────────────────────────────────────────────────────────
    source_path: Path | None = None
    content_hash: str = ""  # SHA256 of source YAML

    def traefik_sub(self) -> str:
        return self.traefik_subdomain or self.key

    def to_catalog_entry(self) -> dict[str, Any]:
        """Compact dict for the UI catalog API."""
        return {
            "key": self.key,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
            "tier": self.tier,
            "icon": self.icon,
            "web_port": self.web_port,
            "linuxserver": self.linuxserver,
            "tags": self.tags,
            "links": self.links,
            "has_gpu": self.gpu is not None,
            "gpu_optional": self.gpu.optional if self.gpu is not None else None,
            "hardware_note": self.hardware_note,
            "start_grace_s": self.start_grace_s or 60,
            "dependencies": {
                "postgres": self.dependencies.postgres,
                "redis": self.dependencies.redis,
                "mariadb": self.dependencies.mariadb,
                "apps": self.dependencies.apps,
            },
            "install_prompts": list(self.install_prompts),
        }


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


# Parser helpers — extracted to loader_parsers.py (file-size discipline).
# Re-exported here for backward compatibility.
# Late import required: loader_parsers imports dataclasses defined above (would be circular at top).
from backend.manifests.loader_parsers import (  # noqa: E402
    _require,
    _parse_ports,
    _parse_volumes,
    _parse_wiring,
    _parse_post_deploy,
    _parse_health,
    _parse_gpu,
    _parse_dependencies,
    _parse_install_prompts,
    _content_hash,
    _filter_capabilities,
    _SAFE_CAPABILITIES,  # noqa: F401  # re-exported for test and external callers
    _HEALTH_REQUIRED_TIERS,  # noqa: F401  # re-exported for test and external callers
)


def _resolve_proxy_config(data: dict[str, Any], key: str) -> dict[str, Any]:
    """#1263 (#990 residual): resolve the abstract reverse-proxy config block.

    `reverse_proxy:` / `proxy:` are the canonical de-Traefik keys; `traefik:` is a
    deprecated backward-compat ALIAS. Only the YAML KEY is de-Traefik-ized — the
    AppManifest fields keep their traefik_* names (renaming those is the #990
    reverse_proxy SlotContract's job, not this cosmetic alias). Canonical wins on
    conflict; a lone deprecated `traefik:` key is accepted but warned."""
    proxy_raw = data.get("reverse_proxy")
    if proxy_raw is None:
        proxy_raw = data.get("proxy")
    if "traefik" in data:
        if proxy_raw is not None:
            # A real misconfiguration (both a canonical key AND the legacy alias) —
            # worth a per-manifest WARNING so it isn't silently merged.
            log.warning(
                "Manifest %r sets a canonical reverse-proxy key AND the deprecated "
                "'traefik:' key — using the canonical key, ignoring 'traefik:'.",
                key,
            )
        else:
            # Lone legacy `traefik:` — accepted SILENTLY. A per-manifest deprecation
            # warning would add one WARNING per catalog app (~86) to every
            # load_all_manifests() at startup, and the #990 DToC explicitly DEFERRED
            # catalog migration, so the nudge isn't yet actionable. The
            # migration-nudge warning belongs with the scheduled catalog migration
            # (mechanical traefik:→reverse_proxy: sweep) / the #990 slot land.
            proxy_raw = data.get("traefik")
    # Harden against a malformed block (e.g. `reverse_proxy: [..]` / a scalar): the
    # downstream `traefik_raw.get(...)` would crash on a non-mapping. The old code
    # (`data.get("traefik", {}) or {}`) had this same latent crash; here we coerce a
    # non-dict to {} (warned) so a bad community manifest degrades to defaults, never
    # a parse crash. A falsy/None value coalesces to {} silently (unchanged).
    if proxy_raw and not isinstance(proxy_raw, dict):
        log.warning(
            "Manifest %r reverse-proxy config is not a mapping (got %s) — ignoring.",
            key,
            type(proxy_raw).__name__,
        )
        return {}
    return proxy_raw or {}


def parse_manifest(path: Path) -> AppManifest:
    """Parse and validate a single manifest YAML file.

    Raises ManifestError with a plain-language message if the file is
    invalid. Never raises raw YAML or Python exceptions to callers.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(path, f"Could not read file: {e}") from e

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise ManifestError(path, f"Invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ManifestError(path, "Manifest must be a YAML mapping, not a list or scalar")

    # Required fields
    key = _require(data, "key", path)
    display_name = _require(data, "display_name", path)
    image = _require(data, "image", path)
    category = _require(data, "category", path)

    if not key.replace("-", "").replace("_", "").isalnum():
        raise ManifestError(
            path, f"key must be alphanumeric (hyphens/underscores ok), got: {key!r}"
        )

    if category not in VALID_CATEGORIES:
        raise ManifestError(
            path, f"Unknown category '{category}'. Valid: {sorted(VALID_CATEGORIES)}"
        )

    tier = int(data.get("tier", 2))
    if tier not in (1, 2):
        raise ManifestError(path, f"tier must be 1 or 2, got: {tier}")

    # Structural sections
    web_port, extra_ports = _parse_ports(data, path)
    config_vol, media_vol, custom_vols = _parse_volumes(data, path)
    wiring = _parse_wiring(data, path)
    post_deploy = _parse_post_deploy(data, path)
    health_checks, self_heal = _parse_health(data, path, tier=tier)
    gpu = _parse_gpu(data)
    deps = _parse_dependencies(data)

    # Warn when a post_deploy wire step declares a wire_type with no handler.
    # This is a WARNING only — manifests continue to load; wiring fails at
    # install time rather than silently deferring forever.
    def _validate_wire_types(manifest_key: str, steps: list[Any]) -> None:
        from backend.manifests.wiring import WIRE_HANDLERS  # late import — avoids circular

        for step in steps:
            if step.step_type == "wire" and step.wire_type:
                if step.wire_type not in WIRE_HANDLERS:
                    log.warning(
                        "Manifest %r declares wire_type=%r which has no registered handler. "
                        "Wiring will fail at install time.",
                        manifest_key,
                        step.wire_type,
                    )

    _validate_wire_types(key, post_deploy)

    traefik_raw = _resolve_proxy_config(data, key)

    env_raw = data.get("env", {}) or {}
    env = {str(k): str(v) for k, v in env_raw.items()}

    install_prompts = _parse_install_prompts(data)

    return AppManifest(
        key=key,
        display_name=display_name,
        description=data.get("description", ""),
        category=category,
        tier=tier,
        icon=data.get("icon", "📦"),
        version=str(data.get("version", "1.0")),
        image=image,
        image_tag=str(data.get("image_tag", "latest")),
        linuxserver=bool(data.get("linuxserver", True)),
        web_port=web_port,
        extra_ports=extra_ports,
        config_volume=config_vol,
        media_volume=media_vol,
        custom_volumes=custom_vols,
        backup_supported=bool(data.get("backup_supported", False)),
        backup_dir=str(data.get("backup_dir", "") or ""),
        backup_verify=str(data.get("backup_verify", "") or ""),
        backup_offhost=bool(data.get("backup_offhost", False)),
        env=env,
        dependencies=deps,
        traefik_enabled=bool(traefik_raw.get("enabled", True)),
        traefik_subdomain=str(traefik_raw.get("subdomain", "")),
        traefik_headers=dict(traefik_raw.get("headers", {})),
        wiring=wiring,
        post_deploy=post_deploy,
        health_checks=health_checks,
        self_heal=self_heal,
        gpu=gpu,
        hardware_note=str(data["hardware_note"]) if data.get("hardware_note") else None,
        companions=list(data.get("companions", []) or []),
        requires=list(data.get("requires", []) or []),
        recommends=list(data.get("recommends", []) or []),
        capabilities=_filter_capabilities(list(data.get("capabilities", []) or []), path),
        security_opt=list(data.get("security_opt", []) or []),
        devices=list(data.get("devices", []) or []),
        extra_config=dict(data.get("extra_config", {}) or {}),
        links=dict(data.get("links", {}) or {}),
        tags=list(data.get("tags", []) or []),
        service_type=str(data.get("service_type", "management")),
        dashboard_icon=str(data.get("dashboard_icon", "") or ""),
        start_grace_s=int(data.get("start_grace_s", 0) or 0),
        install_wait_s=int(data["install_wait_s"])
        if data.get("install_wait_s") is not None
        else None,
        health_grace_s=int(data["health_grace_s"])
        if data.get("health_grace_s") is not None
        else None,
        pull_timeout_s=int(data.get("pull_timeout_s", 600) or 600),
        config_schema=list(data.get("config_schema", []) or []),
        post_install=list(data.get("post_install", []) or []),
        config_defaults=dict(data.get("config_defaults", {}) or {}),
        auto_secrets=list(data.get("auto_secrets", []) or []),
        seed_config=list(data.get("seed_config", []) or []),
        install_prompts=install_prompts,
        tls_cert_path=str(data.get("tls_cert_path", "") or ""),
        source_path=path,
        content_hash=_content_hash(raw_text),
    )


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------


_cache: dict[str, AppManifest] = {}
_cache_loaded_at: float = 0.0  # epoch seconds of last load_all_manifests call
_CACHE_TTL: float = 300.0  # 5 minutes — new community manifests picked up automatically


def load_manifest(key: str, force_reload: bool = False) -> AppManifest:
    """Load a single manifest by app key.

    Checks catalog/apps/<key>.yaml first, then catalog/community/<key>.yaml.
    Caches; force_reload=True picks up changes (e.g. after a community install).
    Raises ManifestError (invalid), KeyError (not found), or PathNotAllowed — the
    last is the traversal guard at the common {key}->path seam (#1041).
    """
    from backend.core.path_guard import safe_component

    safe_component(key, field="app key")
    if key in _cache and not force_reload:
        return _cache[key]

    # Check official catalog first, fall back to community manifests.
    # Community manifests are written by custom/GitHub installs and live at
    # catalog/community/<key>.yaml.  Without this fallback, load_manifest()
    # would only find community apps if load_all_manifests() had already
    # warmed the cache — making smoke tests and health checks unreliable for
    # custom-installed apps.
    app_path = config.catalog_dir / "apps" / f"{key}.yaml"
    if not app_path.exists():
        community_path = config.catalog_dir / "community" / f"{key}.yaml"
        if community_path.exists():
            app_path = community_path
        else:
            raise KeyError(
                f"No manifest found for '{key}'. "
                f"Looked in: {config.catalog_dir / 'apps'} and "
                f"{config.catalog_dir / 'community'}"
            )

    manifest = parse_manifest(app_path)
    if manifest.key != key:
        raise ManifestError(
            app_path, f"Manifest key '{manifest.key}' doesn't match filename '{key}.yaml'."
        )

    _cache[key] = manifest
    return manifest


def load_all_manifests(force_reload: bool = False) -> dict[str, AppManifest]:
    """Load every manifest in catalog/apps/.

    Returns a dict keyed by app key. Invalid manifests are logged as
    warnings and skipped — a bad community manifest doesn't break the catalog.
    """
    global _cache_loaded_at
    import time as _time

    now = _time.monotonic()

    # Fast path: cache is warm and the caller didn't force a reload.
    # Previously the cache was populated but load_all_manifests always re-scanned
    # the YAML files, making every catalog API call take ~350 ms.
    if _cache and not force_reload and (now - _cache_loaded_at) <= _CACHE_TTL:
        return dict(_cache)

    # Cache miss or expired — clear and rebuild from disk.
    if _cache:
        log.debug("Manifest cache expired (%.0fs old) — reloading", now - _cache_loaded_at)
    _cache.clear()

    result: dict[str, AppManifest] = {}

    # Scan official catalog first, then community manifests.
    # Community manifests override official ones if keys match (allows patching).
    scan_dirs = [
        config.catalog_dir / "apps",  # official manifests (baked into image)
        config.catalog_dir / "community",  # user-pulled community manifests
    ]

    for apps_dir in scan_dirs:
        if not apps_dir.exists():
            continue
        for yaml_path in sorted(apps_dir.glob("*.yaml")):
            key = yaml_path.stem
            source = "community" if "community" in str(apps_dir) else "official"
            try:
                manifest = parse_manifest(yaml_path)
                if manifest.key != key:
                    log.warning(
                        "Skipping %s [%s] — manifest key '%s' doesn't match filename",
                        yaml_path.name,
                        source,
                        manifest.key,
                    )
                    continue
                if key in result and source == "community":
                    existing = result[key]
                    existing_src = str(existing.source_path) if existing.source_path else "official"
                    version_delta = ""
                    if existing.version != manifest.version:
                        version_delta = f" (version: {existing.version!r} → {manifest.version!r})"
                    log.warning(
                        "MANIFEST OVERRIDE: community manifest replaces built-in app %r"
                        " | official source: %s"
                        " | community source: %s"
                        "%s"
                        " — review community/manifest for unintended changes",
                        key,
                        existing_src,
                        str(yaml_path),
                        version_delta,
                    )
                result[key] = manifest
                _cache[key] = manifest
            except ManifestError as e:
                log.warning(
                    "Skipping invalid manifest %s [%s]: %s", yaml_path.name, source, e.message
                )

    _cache_loaded_at = _time.monotonic()
    return result


def clear_cache() -> None:
    """Clear the manifest cache. Used in tests and on TTL expiry."""
    global _cache_loaded_at
    _cache.clear()
    _cache_loaded_at = 0.0
