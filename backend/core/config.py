"""backend/core/config.py

Centralised configuration. All env-var reads happen here.
Everything else imports from this module — never os.environ directly.

Operator-env mechanism (``.env`` is authoritative):
``MS_TRUSTED_HOSTS`` / ``DOMAIN`` and the other ``MS_*`` settings are read from
``os.environ``. To make editing the install-dir ``.env`` actually take effect —
as the docs imply — this module loads that ``.env`` into ``os.environ`` at import
time, BEFORE any env read happens. The load is ``override=False``: a value already
present in the real process environment (systemd ``Environment=`` / an operator
shell export) always wins over ``.env``. This makes the mechanism robust to a
stale systemd unit whose ``EnvironmentFile=`` was never reloaded (the actual
an env-precedence failure mode observed in testing) while keeping systemd/shell overrides authoritative.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Sentinel so the dotenv load runs exactly once per process, and so tests can
# reset it (monkeypatch this False) to force a reload against a tmp .env.
_loaded_env_done = False


def _resolve_env_file() -> Path:
    """The .env to load — install-dir/.env, overridable via MS_ENV_FILE.

    Mirrors ``Config.env_file`` but is needed *before* the Config singleton is
    built (the singleton itself reads env that .env may supply).
    """
    env_override = os.environ.get("MS_ENV_FILE")
    if env_override:
        return Path(env_override)
    # repo root == install dir (this file is backend/core/config.py)
    return Path(__file__).parent.parent.parent / ".env"


def load_dotenv(env_path: Path | None = None, *, override: bool = False) -> int:
    """Populate ``os.environ`` from a ``.env`` file (stdlib-only, no dependency).

    Returns the number of keys applied. Parses ``KEY=VALUE`` lines:
    blank lines and ``#`` comments are skipped; an optional leading ``export``
    is stripped; surrounding single/double quotes on the value are removed.
    With ``override=False`` (default) a key already set in ``os.environ`` is
    left untouched — the real process environment wins over the file.
    """
    path = env_path if env_path is not None else _resolve_env_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return 0
    applied = 0
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied += 1
    return applied


def ensure_env_loaded() -> None:
    """Load ``.env`` into ``os.environ`` once per process (idempotent)."""
    global _loaded_env_done
    if _loaded_env_done:
        return
    _loaded_env_done = True
    load_dotenv()


@dataclass(frozen=True)
class Config:
    # ── Paths ──────────────────────────────────────────────────────────────
    data_dir: Path  # writable runtime directory (DB, compose fragments, .env)
    catalog_dir: Path  # read-only catalog YAML files
    static_dir: Path  # compiled frontend assets

    # ── Network ────────────────────────────────────────────────────────────
    bind_host: str
    bind_port: int
    docker_socket: str

    # ── Feature flags ─────────────────────────────────────────────────────
    debug: bool
    # ── Docker host path (for containerized SLOP) ─────────────────────
    # When running inside Docker, compose fragments must reference HOST paths
    # so the Docker daemon can resolve volume mounts correctly.
    # Set MS_HOST_DATA_DIR to the host-side path (left side of volume mount).
    host_data_dir: Path | None = None  # empty = use data_dir
    host_config_dir: Path | None = None  # empty = use config_root

    # ── Derived paths (computed properties) ───────────────────────────────
    @property
    def effective_data_dir(self) -> Path:
        """Path to use in compose fragment volume mounts.
        When containerized: the HOST path (from MS_HOST_DATA_DIR).
        When native: same as data_dir.
        """
        if self.host_data_dir and str(self.host_data_dir) not in (".", ""):
            return self.host_data_dir
        return self.data_dir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def compose_dir(self) -> Path:
        """Where per-service compose fragments are written."""
        return self.data_dir / "compose"

    @property
    def install_dir(self) -> Path:
        """Root of the SLOP installation (repo root)."""
        return Path(__file__).parent.parent.parent

    @property
    def env_file(self) -> Path:
        """The .env file the user edits — also what the service reads.
        Stored in the install dir (not data_dir) so edits persist across
        ms-update without needing to copy files."""
        env_override = os.environ.get("MS_ENV_FILE")
        if env_override:
            return Path(env_override)
        return self.install_dir / ".env"

    @classmethod
    def from_env(cls) -> Config:
        base = Path(__file__).parent.parent.parent  # repo root
        return cls(
            data_dir=Path(os.environ.get("MS_DATA_DIR", str(base / "data"))),
            catalog_dir=Path(os.environ.get("MS_CATALOG_DIR", str(base / "catalog"))),
            static_dir=Path(
                os.environ.get(
                    "MS_STATIC_DIR",
                    str(base / "backend" / "static"),  # Vite outDir in vite.config.ts
                )
            ),
            bind_host=os.environ.get("MS_BIND_HOST", "0.0.0.0"),  # noqa: S104  # nosec B104  # binding to all interfaces is intentional for container deployment
            bind_port=int(os.environ.get("MS_BIND_PORT", "8080")),
            docker_socket=os.environ.get("MS_DOCKER_SOCKET", "unix:///var/run/docker.sock"),
            debug=os.environ.get("MS_DEBUG", "").lower() in ("1", "true", "yes"),
            host_data_dir=Path(os.environ["MS_HOST_DATA_DIR"])
            if os.environ.get("MS_HOST_DATA_DIR")
            else None,
            host_config_dir=Path(os.environ["MS_HOST_CONFIG_DIR"])
            if os.environ.get("MS_HOST_CONFIG_DIR")
            else None,
        )


# Load install-dir .env into os.environ BEFORE building the singleton, so both
# this module's reads AND later os.environ.get(...) reads in backend/api/main.py
# (MS_TRUSTED_HOSTS / DOMAIN) pick up .env. override=False ⇒ real env wins.
ensure_env_loaded()

# Module-level singleton — import and use directly
config = Config.from_env()
