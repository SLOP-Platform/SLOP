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
