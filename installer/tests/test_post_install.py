"""installer/tests/test_post_install.py — tests for post_install.py.

Covers:
  - render(): successful substitution, unfilled-placeholder ValueError, INV-8 invariant
  - _resolve_hostname(): three-step fallback chain
  - write(): atomic temp-file rename, mode 0644, run_chown called
"""

from __future__ import annotations

import stat
import types

import pytest

from installer.post_install import _resolve_hostname, render, write, write_wrapper

# ── Shared fixtures ───────────────────────────────────────────────────────────

_TEMPLATE = (
    "SLOP v<version> install complete\n"
    "========================================\n"
    "\n"
    "Open your browser to:\n"
    "\n"
    "    http://<hostname>:<port>/\n"
    "\n"
    "Install dir:  <install_dir>\n"
    "Data dir:     <data_dir>\n"
    "Installed at: <completed_at>\n"
    "\n"
    "Uninstall:  sudo <install_dir>/bin/slop uninstall\n"
)

_RENDER_KWARGS = {
    "version": "5.0.0",
    "hostname": "192.168.1.10",
    "port": 8080,
    "install_dir": "/opt/slop",
    "data_dir": "/var/lib/slop",
    "completed_at": "2026-01-01T00:00:00Z",
    "template_text": _TEMPLATE,
}


def _run_ok(cmd, **_):
    """Fake run() that always returns rc=0."""
    stdout = ""
    if "hostname" in cmd and "-I" in cmd:
        stdout = "192.168.1.10 fe80::1\n"
    elif "hostname" in cmd and "--fqdn" in cmd:
        stdout = "myhost.example.com\n"
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _run_fail(cmd, **_):
    return types.SimpleNamespace(returncode=1, stdout="", stderr="error")


# ── render() ─────────────────────────────────────────────────────────────────


class TestRender:
    def test_successful_substitution(self):
        result = render(**_RENDER_KWARGS)
        assert "SLOP v5.0.0 install complete" in result
        assert "http://192.168.1.10:8080/" in result
        assert "/opt/slop" in result
        assert "/var/lib/slop" in result
        assert "2026-01-01T00:00:00Z" in result

    def test_install_dir_appears_twice(self):
        result = render(**_RENDER_KWARGS)
        assert result.count("/opt/slop") == 2

    def test_port_as_integer(self):
        result = render(**_RENDER_KWARGS)
        assert ":8080/" in result

    def test_unfilled_placeholder_raises(self):
        bad_template = _TEMPLATE + "\nExtra: <secret_token>\n"
        with pytest.raises(ValueError, match="unfilled placeholders"):
            render(**{**_RENDER_KWARGS, "template_text": bad_template})

    def test_unfilled_error_names_placeholder(self):
        bad_template = "Hello <unknown_field>"
        with pytest.raises(ValueError, match="<unknown_field>"):
            render(**{**_RENDER_KWARGS, "template_text": bad_template})

    def test_inv8_no_placeholder_tokens_remain(self):
        import re

        result = render(**_RENDER_KWARGS)
        unfilled = re.findall(r"<[a-z_]+>", result)
        assert unfilled == [], f"Unfilled tokens found: {unfilled}"

    def test_uses_real_template_by_default(self):
        result = render(
            version="5.0.0",
            hostname="host",
            port=8080,
            install_dir="/inst",
            data_dir="/data",
            completed_at="2026-01-01T00:00:00Z",
        )
        # Template uses bare semver (SLOP <version>), matching the convention in
        # backend/__init__.py::__version__ and /api/ping. The v-prefix is used
        # only for git/release tags, not user-facing display.
        assert "SLOP 5.0.0 install complete" in result

    def test_no_format_braces_misfire(self):
        template_with_braces = _TEMPLATE + "\nInfo: {some_python_var}\n"
        result = render(**{**_RENDER_KWARGS, "template_text": template_with_braces})
        assert "{some_python_var}" in result

    def test_uninstall_uses_wrapper_not_main_py(self):
        result = render(**_RENDER_KWARGS)
        assert "/bin/slop" in result
        assert "installer/main.py" not in result


# ── _resolve_hostname() ───────────────────────────────────────────────────────


class TestResolveHostname:
    def _run_returning(self, hostname_i_out=None, fqdn_out=None):
        """Return a run callable that returns configured results per cmd."""

        def _run(cmd, **_):
            if "-I" in cmd:
                if hostname_i_out is None:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="")
                return types.SimpleNamespace(returncode=0, stdout=hostname_i_out, stderr="")
            if "--fqdn" in cmd:
                if fqdn_out is None:
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="")
                return types.SimpleNamespace(returncode=0, stdout=fqdn_out, stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        return _run

    def test_returns_first_non_loopback_ip(self):
        run = self._run_returning(hostname_i_out="10.0.1.5 fe80::1\n")
        result = _resolve_hostname(run=run)
        assert result == "10.0.1.5"

    def test_skips_loopback_127(self):
        run = self._run_returning(hostname_i_out="127.0.0.1 10.0.2.3\n")
        result = _resolve_hostname(run=run)
        assert result == "10.0.2.3"

    def test_skips_ipv6_loopback(self):
        run = self._run_returning(hostname_i_out="::1 10.0.3.4\n")
        result = _resolve_hostname(run=run)
        assert result == "10.0.3.4"

    def test_falls_back_to_fqdn_when_hostname_i_fails(self):
        run = self._run_returning(hostname_i_out=None, fqdn_out="myhost.example.com\n")
        result = _resolve_hostname(run=run)
        assert result == "myhost.example.com"

    def test_falls_back_to_fqdn_when_hostname_i_all_loopback(self):
        run = self._run_returning(hostname_i_out="127.0.0.1\n", fqdn_out="box.local\n")
        result = _resolve_hostname(run=run)
        assert result == "box.local"

    def test_falls_back_to_localhost_when_both_fail(self):
        run = self._run_returning(hostname_i_out=None, fqdn_out=None)
        result = _resolve_hostname(run=run)
        assert result == "localhost"

    def test_falls_back_to_localhost_on_exception(self):
        def _run_raises(cmd, **_):
            raise OSError("not found")

        result = _resolve_hostname(run=_run_raises)
        assert result == "localhost"

    def test_fqdn_strips_whitespace(self):
        run = self._run_returning(hostname_i_out=None, fqdn_out="  trimmed.host  \n")
        result = _resolve_hostname(run=run)
        assert result == "trimmed.host"


# ── write() ──────────────────────────────────────────────────────────────────


class TestWrite:
    def test_creates_post_install_txt(self, tmp_path):
        chown_calls = []

        def _chown(path):
            chown_calls.append(path)

        write("content", tmp_path, run_chown=_chown)

        dest = tmp_path / "POST_INSTALL.txt"
        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == "content"

    def test_file_mode_is_0644(self, tmp_path):
        write("x", tmp_path, run_chown=lambda p: None)
        dest = tmp_path / "POST_INSTALL.txt"
        mode = stat.S_IMODE(dest.stat().st_mode)
        assert mode == 0o644, f"Expected 0644, got {oct(mode)}"

    def test_run_chown_called_with_path(self, tmp_path):
        chown_calls = []
        write("x", tmp_path, run_chown=chown_calls.append)
        assert len(chown_calls) == 1
        assert chown_calls[0].endswith(".post-install.tmp") or str(tmp_path) in chown_calls[0]

    def test_atomic_rename_no_temp_on_success(self, tmp_path):
        write("content", tmp_path, run_chown=lambda p: None)
        tmp_files = list(tmp_path.glob(".post-install.tmp*"))
        assert tmp_files == [], f"Temp file left behind: {tmp_files}"

    def test_temp_cleaned_up_on_chown_failure(self, tmp_path):
        def _bad_chown(path):
            raise PermissionError("chown failed")

        with pytest.raises(PermissionError):
            write("content", tmp_path, run_chown=_bad_chown)

        tmp_files = list(tmp_path.glob(".post-install.tmp*"))
        assert tmp_files == [], f"Temp file not cleaned up: {tmp_files}"

    def test_overwrites_existing_file(self, tmp_path):
        dest = tmp_path / "POST_INSTALL.txt"
        dest.write_text("old", encoding="utf-8")
        write("new", tmp_path, run_chown=lambda p: None)
        assert dest.read_text(encoding="utf-8") == "new"

    def test_full_content_roundtrip(self, tmp_path):
        content = render(**_RENDER_KWARGS)
        write(content, tmp_path, run_chown=lambda p: None)
        dest = tmp_path / "POST_INSTALL.txt"
        assert dest.read_text(encoding="utf-8") == content


# ── write_wrapper() ───────────────────────────────────────────────────────────


class TestWriteWrapper:
    def test_creates_wrapper_at_bin_slop(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        assert (tmp_path / "bin" / "slop").exists()

    def test_wrapper_has_shebang(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        content = (tmp_path / "bin" / "slop").read_text(encoding="utf-8")
        assert content.startswith("#!/usr/bin/env bash")

    def test_wrapper_sets_pythonpath_to_install_dir(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        content = (tmp_path / "bin" / "slop").read_text(encoding="utf-8")
        assert f"PYTHONPATH={tmp_path}" in content

    def test_wrapper_uses_module_form(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        content = (tmp_path / "bin" / "slop").read_text(encoding="utf-8")
        assert "-m installer.main" in content

    def test_wrapper_exec_uses_venv_python(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        content = (tmp_path / "bin" / "slop").read_text(encoding="utf-8")
        assert str(tmp_path / ".venv" / "bin" / "python3") in content

    def test_wrapper_mode_is_0755(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        path = tmp_path / "bin" / "slop"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o755, f"Expected 0755, got {oct(mode)}"

    def test_run_chown_called_once(self, tmp_path):
        calls = []
        write_wrapper(tmp_path, run_chown=calls.append)
        assert len(calls) == 1

    def test_idempotent_on_rerun(self, tmp_path):
        write_wrapper(tmp_path, run_chown=lambda p: None)
        write_wrapper(tmp_path, run_chown=lambda p: None)
        assert (tmp_path / "bin" / "slop").exists()
