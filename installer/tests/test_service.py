"""installer/tests/test_service.py — unit tests for installer/service.py.

All filesystem and subprocess I/O is mocked via install_service keyword-only
injection.  No real systemctl calls, no real file writes, no real template
file reads.

_render_template is tested directly as a pure function.

Coverage:
  TestRenderTemplate              — install_dir/data_dir substitution correctness
  TestInstallServiceUnitFile      — rendered content written to correct path
  TestInstallServiceSystemctl     — daemon-reload, enable, start called in order
  TestInstallServiceFailureModes  — write failure; systemctl failure propagation
"""

from __future__ import annotations

from pathlib import Path

import pytest

from installer._run import MissingBinaryError
from installer.service import (
    ServiceInstallError,
    SystemctlError,
    _read_template,
    _render_template,
    _run_systemctl,
    install_service,
)

# Sentinel template that exercises both substitution variables.
_TMPL = (
    "[Service]\n"
    "WorkingDirectory={{ install_dir }}\n"
    "Environment=MS_DATA_DIR={{ data_dir }}\n"
    "ExecStart={{ install_dir }}/.venv/bin/uvicorn backend.api.main:app\n"
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _noop(*_args, **_kwargs):
    pass


def _passing_kwargs(template: str = _TMPL) -> dict:
    """Return install_service injectable kwargs for a default-success scenario."""
    return {
        "read_template": lambda: template,
        "write_unit_file": _noop,
        "run_systemctl": _noop,
        "setup_env_file": _noop,
    }


# ── TestRenderTemplate ────────────────────────────────────────────────────────


class TestRenderTemplate:
    def test_substitutes_install_dir(self):
        result = _render_template("WorkingDirectory={{ install_dir }}", "/opt/ms", "/var/lib/ms")
        assert result == "WorkingDirectory=/opt/ms"

    def test_substitutes_data_dir(self):
        result = _render_template(
            "Environment=MS_DATA_DIR={{ data_dir }}", "/opt/ms", "/var/lib/ms"
        )
        assert result == "Environment=MS_DATA_DIR=/var/lib/ms"

    def test_substitutes_all_occurrences_of_install_dir(self):
        tmpl = "WorkingDirectory={{ install_dir }}\nExecStart={{ install_dir }}/.venv/bin/uvicorn"
        result = _render_template(tmpl, "/opt/ms", "/var/lib/ms")
        assert "{{ install_dir }}" not in result
        assert result.count("/opt/ms") == 2

    def test_no_leftover_placeholders(self):
        result = _render_template(_TMPL, "/opt/ms", "/var/lib/ms")
        assert "{{ " not in result
        assert " }}" not in result

    def test_install_dir_value_appears_in_exec_start(self):
        result = _render_template(_TMPL, "/srv/slop", "/data")
        assert "/srv/slop/.venv/bin/uvicorn" in result

    def test_data_dir_value_appears_in_environment(self):
        result = _render_template(_TMPL, "/opt/ms", "/var/lib/slop")
        assert "MS_DATA_DIR=/var/lib/slop" in result


# ── TestInstallServiceUnitFile ────────────────────────────────────────────────


class TestInstallServiceUnitFile:
    def test_unit_file_written(self):
        writes = []
        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = lambda path, content: writes.append((path, content))
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert len(writes) == 1

    def test_unit_written_to_systemd_path(self):
        writes = []
        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = lambda path, content: writes.append((path, content))
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        path, _ = writes[0]
        assert path == Path("/etc/systemd/system/slop.service")

    def test_install_dir_substituted_in_written_content(self):
        writes = []
        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = lambda path, content: writes.append((path, content))
        install_service("/srv/mydir", "/var/lib/ms", **kwargs)
        _, content = writes[0]
        assert "/srv/mydir" in content
        assert "{{ install_dir }}" not in content

    def test_data_dir_substituted_in_written_content(self):
        writes = []
        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = lambda path, content: writes.append((path, content))
        install_service("/opt/ms", "/custom/data", **kwargs)
        _, content = writes[0]
        assert "/custom/data" in content
        assert "{{ data_dir }}" not in content

    def test_template_read_before_write(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["read_template"] = lambda: call_order.append("read") or _TMPL
        kwargs["write_unit_file"] = lambda p, c: call_order.append("write")
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert call_order.index("read") < call_order.index("write")


# ── TestInstallServiceSystemctl ───────────────────────────────────────────────


class TestInstallServiceSystemctl:
    def test_daemon_reload_called(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: calls.append(sub)
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert "daemon-reload" in calls

    def test_enable_called(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: calls.append((sub, unit))
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert any(sub == "enable" for sub, _ in calls)

    def test_start_called(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: calls.append((sub, unit))
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert any(sub == "start" for sub, _ in calls)

    def test_enable_targets_slop_unit(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: calls.append((sub, unit))
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        enable_calls = [(s, u) for s, u in calls if s == "enable"]
        assert enable_calls == [("enable", "slop")]

    def test_start_targets_slop_unit(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: calls.append((sub, unit))
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        start_calls = [(s, u) for s, u in calls if s == "start"]
        assert start_calls == [("start", "slop")]

    def test_daemon_reload_before_enable(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: call_order.append(sub)
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert call_order.index("daemon-reload") < call_order.index("enable")

    def test_enable_before_start(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: call_order.append(sub)
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert call_order.index("enable") < call_order.index("start")

    def test_full_systemctl_order(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = lambda sub, unit: call_order.append(sub)
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert call_order == ["daemon-reload", "enable", "start"]

    def test_write_before_daemon_reload(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = lambda p, c: call_order.append("write")
        kwargs["run_systemctl"] = lambda sub, unit: call_order.append(sub)
        install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert call_order.index("write") < call_order.index("daemon-reload")


# ── TestInstallServiceFailureModes ────────────────────────────────────────────


class TestInstallServiceFailureModes:
    def test_write_failure_propagates(self):
        def fail_write(path, content):
            raise ServiceInstallError("permission denied")

        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = fail_write
        with pytest.raises(ServiceInstallError):
            install_service("/opt/ms", "/var/lib/ms", **kwargs)

    def test_write_failure_skips_systemctl(self):
        calls = []

        def fail_write(path, content):
            raise ServiceInstallError("permission denied")

        kwargs = _passing_kwargs()
        kwargs["write_unit_file"] = fail_write
        kwargs["run_systemctl"] = lambda sub, unit: calls.append(sub)
        with pytest.raises(ServiceInstallError):
            install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert calls == []

    def test_daemon_reload_failure_propagates(self):
        def fail_systemctl(sub, unit):
            if sub == "daemon-reload":
                raise SystemctlError("daemon-reload failed")

        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = fail_systemctl
        with pytest.raises(SystemctlError):
            install_service("/opt/ms", "/var/lib/ms", **kwargs)

    def test_daemon_reload_failure_skips_enable(self):
        calls = []

        def fail_on_reload(sub, unit):
            calls.append(sub)
            if sub == "daemon-reload":
                raise SystemctlError("failed")

        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = fail_on_reload
        with pytest.raises(SystemctlError):
            install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert "enable" not in calls

    def test_enable_failure_skips_start(self):
        calls = []

        def fail_on_enable(sub, unit):
            calls.append(sub)
            if sub == "enable":
                raise SystemctlError("failed")

        kwargs = _passing_kwargs()
        kwargs["run_systemctl"] = fail_on_enable
        with pytest.raises(SystemctlError):
            install_service("/opt/ms", "/var/lib/ms", **kwargs)
        assert "start" not in calls


# ── TestServiceBoundaryProbe ──────────────────────────────────────────────────


class TestServiceBoundaryProbe:
    """F11/F14 boundary tests — systemctl absent surfaces MissingBinaryError;
    missing template file surfaces ServiceInstallError.
    """

    def test_run_systemctl_raises_on_systemctl_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("systemctl")):
            with pytest.raises(MissingBinaryError, match="systemctl"):
                _run_systemctl("daemon-reload", "")

    def test_read_template_raises_service_install_error_on_missing_file(self):
        from unittest.mock import patch

        with patch("installer.service._TEMPLATE_FILE", new=Path("/nonexistent/slop.service.j2")):
            with pytest.raises(ServiceInstallError, match="template"):
                _read_template()
