"""backend/core/reality_view.py

RealityView — GROUND-truth observation of the *running SLOP instance*.

This module is part of the SLOP AI Agent (executive-manager) surface.  It
assembles a small, well-formed JSON object describing facts the process can
observe *about itself* by touching physics (the OS, the filesystem, the live
process environment).  This is **GROUND** data: every field is observed from the
running process and a green value can go red against physics — it is never
text-vs-text (XREF).

HARD scope rule (two-owner firewall): this module is RUNTIME-ONLY.  It observes
the running instance and MUST NEVER read or adjudicate docs (CLAUDE.md,
BACKLOG, memory files, …).  It reads only live process / OS / filesystem state.

PINNED schema (consumers must not change without updating all callers)::

    {
      "schema_version": 1,
      "observed_at": "<iso8601>",
      "bound_port": <int>,
      "install_dir_is_git": <bool>,
      "install_dir_owner": "<str>",
      "env_sources": {"<VAR>": "environ" | "dotenv" | "unset", ...}
    }

The key names and the value vocabulary ("environ" / "dotenv" / "unset") are a
pinned contract.  Do NOT change them here unilaterally — any change is a
contract renegotiation with all callers.

Every physical observation goes through a small, individually-fakeable helper
so unit tests inject fakes (tmp_path dirs, fake stat, fake environ) and never
touch the real host or write a real ``.env``.
"""

from __future__ import annotations

import os
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterable, Mapping

# Pinned schema version for the RealityView object.
REALITY_SCHEMA_VERSION: int = 1

# Probe logic-version — mirrors slop-reality-probe.PROBE_VERSION. The host probe
# and this runtime view are two emitters of the same PINNED schema; keeping the
# field present here prevents schema drift between them. (The doc-vs-reality
# reconciler consumes the HOST probe's value as its stale-host guard; this runtime
# view feeds the health API, which does not currently gate on it.)
REALITY_PROBE_VERSION: int = 1

# Pinned value vocabulary for env_sources entries.
ENV_SOURCE_ENVIRON: str = "environ"
ENV_SOURCE_DOTENV: str = "dotenv"
ENV_SOURCE_UNSET: str = "unset"


# ---------------------------------------------------------------------------
# Individually-fakeable physical observers
# ---------------------------------------------------------------------------


def observe_install_dir_is_git(install_dir: Path) -> bool:
    """True iff ``install_dir`` looks like a git checkout (``.git`` present).

    A worktree records ``.git`` as a file (a gitdir pointer) rather than a
    directory, so we accept either.  Pure over its argument — tests pass a
    tmp_path.
    """
    return (install_dir / ".git").exists()


def observe_install_dir_owner(
    install_dir: Path,
    *,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    getpwuid_fn: Callable[[int], object] | None = None,
) -> str:
    """Login name of the user owning ``install_dir``.

    ``stat_fn`` and ``getpwuid_fn`` are injectable so tests can fake both the
    stat call and the passwd lookup without a real host user.  On any failure
    (missing passwd entry, platform without ``pwd``) falls back to the numeric
    uid as a string, and to "unknown" if even stat fails.
    """
    _stat = stat_fn if stat_fn is not None else os.stat
    try:
        uid = _stat(install_dir).st_uid
    except OSError:
        return "unknown"
    if getpwuid_fn is None:
        try:
            import pwd

            getpwuid_fn = pwd.getpwuid
        except ImportError:  # pragma: no cover - non-POSIX
            return str(uid)
    try:
        entry = getpwuid_fn(uid)
        # pwd.struct_passwd.pw_name, or any object exposing pw_name
        name = getattr(entry, "pw_name", None)
        return str(name) if name is not None else str(uid)
    except (KeyError, Exception):  # any lookup failure → numeric
        return str(uid)


def classify_env_source(
    var: str,
    environ: Mapping[str, str],
    dotenv_values: Mapping[str, str],
) -> str:
    """Classify which source actually populated ``var`` — pure function.

    Mirrors the loading order in ``backend/core/config.py``: ``.env`` is loaded
    into ``os.environ`` with ``override=False``, so a value already present in
    the real process environment wins over the file.  Given the FINAL
    ``environ`` plus the values parsed from the ``.env`` file we classify:

      * not in ``environ``                              → "unset"
      * in ``environ`` and the value matches ``.env``   → "dotenv"
        (the file supplied it, or a process value equal to the file's — either
        way the effective value reflects the file)
      * in ``environ`` but value differs from / absent
        in ``.env``                                     → "environ"
        (a pre-existing process value won, per override=False, or the file
        never mentioned it)

    Vocabulary is PINNED: returns exactly "environ" | "dotenv" | "unset".
    """
    if var not in environ:
        return ENV_SOURCE_UNSET
    if var in dotenv_values and dotenv_values[var] == environ[var]:
        return ENV_SOURCE_DOTENV
    return ENV_SOURCE_ENVIRON


def observe_env_sources(
    var_names: Iterable[str],
    environ: Mapping[str, str],
    dotenv_values: Mapping[str, str],
) -> dict[str, str]:
    """Map each var name to its pinned source vocabulary value."""
    return {name: classify_env_source(name, environ, dotenv_values) for name in var_names}


# ---------------------------------------------------------------------------
# Vars observed for provenance
# ---------------------------------------------------------------------------


# SLOP-managed wizard env vars — kept in core to avoid importing from the API
# layer (which would create a circular dependency: core → api → core).
# Keep in sync with backend/api/apps.py::_SLOP_MANAGED_VARS.
_SLOP_MANAGED_VARS: frozenset[str] = frozenset(
    {
        "PUID",
        "PGID",
        "TZ",
        "DOMAIN",
        "CONFIG_ROOT",
        "MEDIA_ROOT",
        "CF_TUNNEL_TOKEN",
        "CF_DNS_API_TOKEN",
        "TAILSCALE_AUTH_KEY",
        "TINYAUTH_USERNAME",
        "TINYAUTH_PASSWORD",
        "TINYAUTH_AUTH_USERS",
        "VPN_TYPE",
        "VPN_SERVICE_PROVIDER",
        "WIREGUARD_PRIVATE_KEY",
        "OPENVPN_USER",
        "OPENVPN_PASSWORD",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
    }
)


def reality_var_names() -> list[str]:
    """The env vars whose provenance the RealityView reports.

    The SLOP-managed wizard vars plus the deploy-relevant operator vars
    (the ``MS_*`` knobs that ``core/config.py`` reads).
    """
    operator = (
        "MS_TRUSTED_HOSTS",
        "MS_BIND_HOST",
        "MS_BIND_PORT",
        "MS_DATA_DIR",
        "MS_ENV_FILE",
        "MS_DEBUG",
    )
    return sorted(_SLOP_MANAGED_VARS | set(operator))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_reality_view(
    *,
    bound_port: int,
    install_dir: Path,
    environ: Mapping[str, str],
    dotenv_values: Mapping[str, str],
    var_names: Iterable[str] | None = None,
    now: datetime | None = None,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    getpwuid_fn: Callable[[int], object] | None = None,
) -> dict[str, Any]:
    """Assemble the PINNED RealityView object from injected observations.

    Pure over its arguments — every physical input (port, install_dir,
    environ, dotenv values, clock, stat, passwd lookup) is injected, so unit
    tests run entirely on fakes / tmp_path with no real host access.
    """
    names = list(var_names) if var_names is not None else reality_var_names()
    observed_at = (now or datetime.now(UTC)).isoformat()
    return {
        "schema_version": REALITY_SCHEMA_VERSION,
        "probe_version": REALITY_PROBE_VERSION,
        "observed_at": observed_at,
        "bound_port": int(bound_port),
        "install_dir_is_git": observe_install_dir_is_git(install_dir),
        "install_dir_owner": observe_install_dir_owner(
            install_dir, stat_fn=stat_fn, getpwuid_fn=getpwuid_fn
        ),
        "env_sources": observe_env_sources(names, environ, dotenv_values),
    }


def _read_dotenv_values() -> dict[str, str]:
    """Parse the real install-dir ``.env`` into a {KEY: VALUE} dict.

    Reuses the exact parsing semantics of ``core.config.load_dotenv`` but does
    NOT mutate ``os.environ`` — it only reports what the file declares, for
    provenance comparison.  Missing / unreadable file → empty dict.
    """
    from backend.core.config import _resolve_env_file

    path = _resolve_env_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return {}
    values: dict[str, str] = {}
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
        values[key] = value
    return values


def assemble_live_reality_view() -> dict[str, Any]:
    """Build a RealityView from the LIVE running instance (production path).

    Touches physics: the live ``config`` singleton (bound port, install dir),
    the real process ``os.environ``, and the real ``.env`` file (read-only,
    for provenance only).  Never writes anything.  Never raises — on any
    failure it returns a minimal well-formed view so the health surface stays
    up.
    """
    try:
        from backend.core.config import config

        return build_reality_view(
            bound_port=config.bind_port,
            install_dir=config.install_dir,
            environ=dict(os.environ),
            dotenv_values=_read_dotenv_values(),
        )
    except Exception:  # the view must never break health
        return {
            "schema_version": REALITY_SCHEMA_VERSION,
            "probe_version": REALITY_PROBE_VERSION,
            "observed_at": datetime.now(UTC).isoformat(),
            "bound_port": 0,
            "install_dir_is_git": False,
            "install_dir_owner": "unknown",
            "env_sources": {},
        }
