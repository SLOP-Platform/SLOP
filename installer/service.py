"""installer/service.py — systemd unit installation step for the v5 installer.

install_service() renders installer/templates/slop.service.j2, writes it
to /etc/systemd/system/slop.service, then runs systemctl daemon-reload,
enable, and start.

Data-dir wiring decision (ADR 0013 §1 boundary 4 / Step 2.7):
  Environment=MS_DATA_DIR={{ data_dir }} in the unit file.
  Rationale: backend/core/config.py already reads MS_DATA_DIR from os.environ;
  an inline Environment= directive is the simplest mechanism (no extra files,
  no extra install steps) and satisfies the ADR contract that the path flows
  installer → state file → systemd unit → backend.

Entry-point correction vs. V5_INSTALLER_PLAN.md Step 2.7.a placeholder:
  The plan named 'ExecStart=<install_dir>/.venv/bin/python -m backend.main'.
  Reality (confirmed from backend/api/main.py): the app is a uvicorn ASGI app
  exposed as 'backend.api.main:app'.  Correct ExecStart:
  {{ install_dir }}/.venv/bin/uvicorn backend.api.main:app --host 0.0.0.0 --port 8080
  The plan's Step 2.7.a checkbox is marked DONE with this correction noted.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from installer._run import run_required

_UNIT_PATH: Path = Path("/etc/systemd/system/slop.service")
_UNIT_NAME: str = "slop"
_TEMPLATE_FILE: Path = Path(__file__).parent / "templates" / "slop.service.j2"

# ── #868 P3: scheduled-backup systemd timer (host-native; §7 design decision) ──
# A oneshot service + timer that runs `ms-backup --all` on a cadence. systemd timer (not an
# in-app scheduler) so backup liveness survives a SLOP restart and does not couple to the
# agent-runtime-only process — see docs/BACKUP-PRODUCT-868-DESIGN.md §7.
_BACKUP_SERVICE_PATH: Path = Path("/etc/systemd/system/ms-backup.service")
_BACKUP_TIMER_PATH: Path = Path("/etc/systemd/system/ms-backup.timer")
_BACKUP_TIMER_NAME: str = "ms-backup.timer"
_BACKUP_SERVICE_TEMPLATE: Path = Path(__file__).parent / "templates" / "ms-backup.service.j2"
_BACKUP_TIMER_TEMPLATE: Path = Path(__file__).parent / "templates" / "ms-backup.timer.j2"
# systemd OnCalendar for the timer FIRE frequency. The timer fires hourly; `--due-only` (#1284)
# filters each tick down to the apps actually due per `backup_cadence_hours` (default 24h) — so the
# effective per-app cadence is a settings write, not a unit re-render. Hourly is the tick, not the
# backup frequency.
_DEFAULT_BACKUP_CADENCE: str = "hourly"


# ── Error classes ─────────────────────────────────────────────────────────────


class ServiceError(Exception):
    pass


class ServiceInstallError(ServiceError):
    pass


class SystemctlError(ServiceError):
    pass


# ── Template rendering (pure — not injected) ──────────────────────────────────


def _render_template(template_text: str, install_dir: str, data_dir: str) -> str:
    """Substitute {{ install_dir }} and {{ data_dir }} in template_text.

    Uses simple str.replace() rather than Jinja2 — jinja2 is not a system
    Python dependency.  The .j2 extension signals the template syntax so a
    future upgrade to real Jinja2 is a one-line change here.
    """
    return template_text.replace("{{ install_dir }}", install_dir).replace(
        "{{ data_dir }}", data_dir
    )


# ── I/O helpers (replaceable in tests via install_service kwargs) ─────────────


def _read_template() -> str:
    try:
        return _TEMPLATE_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServiceInstallError(
            f"Could not read service template at {_TEMPLATE_FILE}: {exc}. "
            "The installer package may be incomplete — reinstall from source."
        ) from exc


def _write_unit_file(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ServiceInstallError(f"Could not write systemd unit to {path}: {exc}") from exc


def _run_systemctl(subcommand: str, unit: str) -> None:
    cmd = ["systemctl", subcommand]
    if unit:
        cmd.append(unit)
    result = run_required(cmd)
    if result.returncode != 0:
        raise SystemctlError(
            f"systemctl {subcommand}"
            + (f" {unit}" if unit else "")
            + f" failed (exit {result.returncode}): {result.stderr.strip()}"
        )


# ── Public entry point ────────────────────────────────────────────────────────


def _setup_env_file(install_dir: Path) -> None:
    """Pre-create .env owned by the service user so the wizard can write to it.

    The install dir is root-owned; without this the slop user gets EPERM.
    """
    import shutil as _shutil

    env_file = Path(install_dir) / ".env"
    if not env_file.exists():
        env_file.touch(mode=0o600)
    _shutil.chown(env_file, user="slop", group="slop")


def install_service(
    install_dir,
    data_dir,
    *,
    read_template: Callable[[], str] = _read_template,
    write_unit_file: Callable[[Path, str], None] = _write_unit_file,
    run_systemctl: Callable[[str, str], None] = _run_systemctl,
    setup_env_file: Callable[[Path], None] = _setup_env_file,
) -> None:
    """Render the systemd unit template and activate the slop service.

    Writes install_dir and data_dir into the template, installs the unit at
    /etc/systemd/system/slop.service, then runs:
      systemctl daemon-reload
      systemctl enable slop
      systemctl start slop

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers pass only install_dir and data_dir.
    """
    template_text = read_template()
    content = _render_template(template_text, str(install_dir), str(data_dir))
    write_unit_file(_UNIT_PATH, content)

    # Pre-create .env owned by the service user so the wizard can write to it.
    # The install dir is root-owned; without this the slop user gets EPERM.
    setup_env_file(Path(install_dir))

    run_systemctl("daemon-reload", "")
    run_systemctl("enable", _UNIT_NAME)
    run_systemctl("start", _UNIT_NAME)


# ── #868 P3: scheduled-backup timer install (advisory; never aborts the install) ──


def _render_backup_template(
    template_text: str, install_dir: str, data_dir: str, cadence: str
) -> str:
    """Substitute {{ install_dir }}, {{ data_dir }}, {{ cadence }} in a backup unit template."""
    return (
        template_text.replace("{{ install_dir }}", install_dir)
        .replace("{{ data_dir }}", data_dir)
        .replace("{{ cadence }}", cadence)
    )


def _read_backup_templates() -> tuple[str, str]:
    """Read (service, timer) template text; raise ServiceInstallError on a missing template."""
    try:
        return (
            _BACKUP_SERVICE_TEMPLATE.read_text(encoding="utf-8"),
            _BACKUP_TIMER_TEMPLATE.read_text(encoding="utf-8"),
        )
    except OSError as exc:
        raise ServiceInstallError(
            f"Could not read backup-timer template ({_BACKUP_SERVICE_TEMPLATE} / "
            f"{_BACKUP_TIMER_TEMPLATE}): {exc}. The installer package may be incomplete."
        ) from exc


def install_backup_timer(
    install_dir,
    data_dir,
    *,
    cadence: str = _DEFAULT_BACKUP_CADENCE,
    read_templates: Callable[[], tuple[str, str]] = _read_backup_templates,
    write_unit_file: Callable[[Path, str], None] = _write_unit_file,
    run_systemctl: Callable[[str, str], None] = _run_systemctl,
) -> None:
    """Render + activate the #868 P3 scheduled-backup systemd timer.

    Writes ms-backup.service (oneshot `ms-backup --all`) + ms-backup.timer (OnCalendar=cadence)
    to /etc/systemd/system, then:
      systemctl daemon-reload
      systemctl enable ms-backup.timer
      systemctl start  ms-backup.timer

    The TIMER is enabled/started (not the oneshot service — the timer triggers it). Scheduled
    backup is RECOVERABILITY, not availability (design §7/§9): the caller treats a failure here
    as advisory and does NOT abort the install. The keyword-only I/O args exist for test injection.
    """
    service_text, timer_text = read_templates()
    write_unit_file(
        _BACKUP_SERVICE_PATH,
        _render_backup_template(service_text, str(install_dir), str(data_dir), cadence),
    )
    write_unit_file(
        _BACKUP_TIMER_PATH,
        _render_backup_template(timer_text, str(install_dir), str(data_dir), cadence),
    )
    run_systemctl("daemon-reload", "")
    run_systemctl("enable", _BACKUP_TIMER_NAME)
    run_systemctl("start", _BACKUP_TIMER_NAME)
