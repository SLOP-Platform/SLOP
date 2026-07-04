"""installer/tests/test_prereq.py — unit tests for installer/prereq.py.

All I/O is mocked via check_prereqs keyword-only injection arguments; no
real /proc reads, no real socket binds, no real disk calls happen here.

Coverage per V5_INSTALLER_PLAN.md Step 1.4.b:
  TestParseKernelVersion  — pure version-string parsing
  TestKernelCheck         — kernel ≥ 5.10 pass/fail, message anatomy
  TestDiskCheck           — disk ≥ 10 GiB pass/fail, ancestor walk, message
  TestPortCheck           — backend + proxy ports pass/fail, message anatomy
  TestRootCheck           — euid == 0 pass/fail
  TestSystemdCheck        — /proc/1/comm == "systemd" pass/fail/unreadable
  TestCheckPrereqs        — integration: correct count, no short-circuit,
                            all-pass, all-fail, port argument wiring
  TestDockerCheck         — docker engine present/absent/old-version, message anatomy
  TestComposeCheck        — docker compose plugin present/absent
"""

from __future__ import annotations

from pathlib import Path


from installer.prereq import (
    PrereqFinding,
    _parse_kernel_version,
    check_prereqs,
)

# ── Shared mock callables for check_prereqs ───────────────────────────────────

_PASS_KERNEL: str = "6.6.87"
_PASS_DISK_BYTES: int = 20 * 1024**3  # 20 GiB — well above 10 GiB threshold
_PASS_UID: int = 0
_PASS_INIT: str = "systemd"
_PASS_DOCKER_VERSION: str = "27.0.3"


def _passing_kwargs(install_path: str = "/tmp/install", port: int = 8080) -> dict:
    """Return check_prereqs kwargs where every single check passes."""
    return {
        "install_path": install_path,
        "port": port,
        "read_kernel_release": lambda: _PASS_KERNEL,
        "get_disk_free_bytes": lambda p: _PASS_DISK_BYTES,
        "is_port_free": lambda port: True,
        "get_effective_uid": lambda: _PASS_UID,
        "read_init_comm": lambda: _PASS_INIT,
        "get_docker_version": lambda: _PASS_DOCKER_VERSION,
        "has_compose_plugin": lambda: True,
    }


# ── TestParseKernelVersion ────────────────────────────────────────────────────


class TestParseKernelVersion:
    def test_standard_linux(self):
        assert _parse_kernel_version("5.15.0-100-generic") == (5, 15)

    def test_wsl2_kernel(self):
        assert _parse_kernel_version("6.6.87.2-microsoft-standard-WSL2") == (6, 6)

    def test_exactly_at_minimum(self):
        assert _parse_kernel_version("5.10.0") == (5, 10)

    def test_below_minimum(self):
        assert _parse_kernel_version("5.4.219") == (5, 4)

    def test_major_only(self):
        assert _parse_kernel_version("6") == (6, 0)

    def test_non_numeric_returns_zeros(self):
        assert _parse_kernel_version("unknown-kernel") == (0, 0)

    def test_empty_string_returns_zeros(self):
        assert _parse_kernel_version("") == (0, 0)


# ── TestKernelCheck ───────────────────────────────────────────────────────────


class TestKernelCheck:
    def _kernel_finding(self, release: str) -> PrereqFinding:
        findings = check_prereqs(**{**_passing_kwargs(), "read_kernel_release": lambda: release})
        return next(f for f in findings if f.name == "kernel version")

    def test_modern_kernel_passes(self):
        f = self._kernel_finding("6.6.87-generic")
        assert f.ok is True
        assert f.remediation == ""

    def test_exactly_minimum_passes(self):
        f = self._kernel_finding("5.10.0")
        assert f.ok is True

    def test_above_minimum_minor_passes(self):
        f = self._kernel_finding("5.15.0-100-generic")
        assert f.ok is True

    def test_below_minimum_fails(self):
        f = self._kernel_finding("5.4.219")
        assert f.ok is False

    def test_very_old_kernel_fails(self):
        f = self._kernel_finding("4.19.0-amd64")
        assert f.ok is False

    def test_fail_message_names_kernel(self):
        f = self._kernel_finding("5.4.0-200-generic")
        assert "5.4.0-200-generic" in f.remediation

    def test_fail_message_names_minimum(self):
        f = self._kernel_finding("5.4.0")
        assert "5.10" in f.remediation

    def test_name_field(self):
        f = self._kernel_finding("6.6.0")
        assert f.name == "kernel version"


# ── TestDiskCheck ─────────────────────────────────────────────────────────────


class TestDiskCheck:
    _TEN_GIB = 10 * 1024**3

    def _disk_finding(self, free_bytes: int, install_path: str = "/tmp/install") -> PrereqFinding:
        findings = check_prereqs(
            **{
                **_passing_kwargs(install_path=install_path),
                "get_disk_free_bytes": lambda p: free_bytes,
            }
        )
        return next(f for f in findings if f.name == "disk space")

    def test_exactly_10_gib_passes(self):
        f = self._disk_finding(self._TEN_GIB)
        assert f.ok is True

    def test_above_threshold_passes(self):
        f = self._disk_finding(self._TEN_GIB + 1)
        assert f.ok is True

    def test_one_byte_below_fails(self):
        f = self._disk_finding(self._TEN_GIB - 1)
        assert f.ok is False

    def test_zero_bytes_fails(self):
        f = self._disk_finding(0)
        assert f.ok is False

    def test_fail_message_mentions_10_gib(self):
        f = self._disk_finding(5 * 1024**3)
        assert "10 GiB" in f.remediation

    def test_fail_message_names_install_path(self, tmp_path):
        f = self._disk_finding(1024, install_path=str(tmp_path))
        assert str(tmp_path) in f.remediation

    def test_name_field(self):
        f = self._disk_finding(20 * 1024**3)
        assert f.name == "disk space"

    def test_get_disk_free_bytes_called_with_existing_path(self, tmp_path):
        """When install_path exists, mock receives install_path itself."""
        seen_paths: list[Path] = []

        def record_path(p: Path) -> int:
            seen_paths.append(p)
            return 20 * 1024**3

        check_prereqs(
            **{**_passing_kwargs(install_path=str(tmp_path)), "get_disk_free_bytes": record_path}
        )
        assert seen_paths == [tmp_path]

    def test_ancestor_walk_when_path_absent(self, tmp_path):
        """When install_path doesn't exist, mock receives an existing ancestor."""
        nonexistent = tmp_path / "does" / "not" / "exist"
        seen_paths: list[Path] = []

        def record_path(p: Path) -> int:
            seen_paths.append(p)
            return 20 * 1024**3

        check_prereqs(
            **{**_passing_kwargs(install_path=str(nonexistent)), "get_disk_free_bytes": record_path}
        )
        assert len(seen_paths) == 1
        # The path passed to the mock must exist (it's the nearest ancestor)
        assert seen_paths[0].exists()
        # And install_path must be a descendant of the path passed
        assert str(nonexistent).startswith(str(seen_paths[0]))


# ── TestPortCheck ─────────────────────────────────────────────────────────────


class TestPortCheck:
    def _port_findings(self, free_ports: set[int], port: int = 8080) -> list[PrereqFinding]:
        findings = check_prereqs(
            **{**_passing_kwargs(port=port), "is_port_free": lambda p: p in free_ports}
        )
        return [f for f in findings if f.name.startswith("port")]

    def test_all_ports_free_all_pass(self):
        for f in self._port_findings({8080, 80, 443}):
            assert f.ok is True

    def test_backend_port_occupied_fails(self):
        pf = self._port_findings({80, 443})  # 8080 missing
        f = next(f for f in pf if "backend" in f.name)
        assert f.ok is False

    def test_port_80_occupied_fails(self):
        pf = self._port_findings({8080, 443})  # 80 missing
        f = next(f for f in pf if "http" in f.name and "https" not in f.name)
        assert f.ok is False

    def test_port_443_occupied_fails(self):
        pf = self._port_findings({8080, 80})  # 443 missing
        f = next(f for f in pf if "https" in f.name)
        assert f.ok is False

    def test_custom_backend_port_checked(self):
        pf = self._port_findings({80, 443}, port=9090)  # 9090 is "free" list absent
        backend = next(f for f in pf if "backend" in f.name)
        assert backend.ok is False
        assert "9090" in backend.name

    def test_custom_backend_port_free_passes(self):
        pf = self._port_findings({9090, 80, 443}, port=9090)
        backend = next(f for f in pf if "backend" in f.name)
        assert backend.ok is True

    def test_fail_message_names_port(self):
        pf = self._port_findings({80, 443})
        f = next(f for f in pf if "backend" in f.name)
        assert "8080" in f.remediation

    def test_port_finding_names(self):
        pf = self._port_findings({8080, 80, 443})
        names = [f.name for f in pf]
        assert any("8080" in n and "backend" in n for n in names)
        assert any("80" in n and "http" in n for n in names)
        assert any("443" in n and "https" in n for n in names)

    def test_three_port_findings_returned(self):
        pf = self._port_findings({8080, 80, 443})
        assert len(pf) == 3


# ── TestRootCheck ─────────────────────────────────────────────────────────────


class TestRootCheck:
    def _root_finding(self, uid: int) -> PrereqFinding:
        findings = check_prereqs(**{**_passing_kwargs(), "get_effective_uid": lambda: uid})
        return next(f for f in findings if f.name == "root access")

    def test_root_uid_passes(self):
        f = self._root_finding(0)
        assert f.ok is True
        assert f.remediation == ""

    def test_nonzero_uid_fails(self):
        f = self._root_finding(1000)
        assert f.ok is False

    def test_fail_message_mentions_sudo(self):
        f = self._root_finding(1000)
        assert "sudo" in f.remediation.lower()

    def test_name_field(self):
        f = self._root_finding(0)
        assert f.name == "root access"


# ── TestSystemdCheck ──────────────────────────────────────────────────────────


class TestSystemdCheck:
    def _systemd_finding(self, comm: str | None) -> PrereqFinding:
        if comm is None:

            def raise_oserror() -> str:
                raise OSError("no such file")

            read_fn = raise_oserror
        else:

            def read_fn():
                return comm

        findings = check_prereqs(**{**_passing_kwargs(), "read_init_comm": read_fn})
        return next(f for f in findings if f.name == "systemd init")

    def test_systemd_passes(self):
        f = self._systemd_finding("systemd")
        assert f.ok is True
        assert f.remediation == ""

    def test_sysvinit_fails(self):
        f = self._systemd_finding("init")
        assert f.ok is False

    def test_openrc_fails(self):
        f = self._systemd_finding("openrc-init")
        assert f.ok is False

    def test_unreadable_proc_comm_fails(self):
        f = self._systemd_finding(None)  # triggers OSError
        assert f.ok is False

    def test_fail_message_names_actual_comm(self):
        f = self._systemd_finding("runit")
        assert "runit" in f.remediation

    def test_fail_message_mentions_systemd(self):
        f = self._systemd_finding("init")
        assert "systemd" in f.remediation

    def test_name_field(self):
        f = self._systemd_finding("systemd")
        assert f.name == "systemd init"


# ── TestCheckPrereqs (integration) ───────────────────────────────────────────


class TestCheckPrereqs:
    def test_all_pass_returns_nine_findings(self):
        findings = check_prereqs(**_passing_kwargs())
        assert len(findings) == 9

    def test_all_pass_all_ok(self):
        findings = check_prereqs(**_passing_kwargs())
        assert all(f.ok for f in findings)

    def test_all_fail_still_returns_nine_findings(self):
        """No short-circuit: all 9 checks run even when every one fails."""
        findings = check_prereqs(
            install_path="/tmp/install",
            port=8080,
            read_kernel_release=lambda: "4.14.0",
            get_disk_free_bytes=lambda p: 0,
            is_port_free=lambda port: False,
            get_effective_uid=lambda: 1000,
            read_init_comm=lambda: "init",
            get_docker_version=lambda: None,
            has_compose_plugin=lambda: False,
        )
        assert len(findings) == 9
        assert all(not f.ok for f in findings)

    def test_no_short_circuit_on_first_failure(self):
        """Kernel fails, but all remaining checks still run and are recorded."""
        call_log: list[str] = []

        def failing_kernel() -> str:
            call_log.append("kernel")
            return "4.14.0"

        def logging_disk(p: Path) -> int:
            call_log.append("disk")
            return 20 * 1024**3

        def logging_port(port: int) -> bool:
            call_log.append(f"port:{port}")
            return True

        def logging_uid() -> int:
            call_log.append("uid")
            return 0

        def logging_init() -> str:
            call_log.append("init")
            return "systemd"

        def logging_docker() -> str | None:
            call_log.append("docker")
            return "27.0.3"

        def logging_compose() -> bool:
            call_log.append("compose")
            return True

        check_prereqs(
            install_path="/tmp/install",
            read_kernel_release=failing_kernel,
            get_disk_free_bytes=logging_disk,
            is_port_free=logging_port,
            get_effective_uid=logging_uid,
            read_init_comm=logging_init,
            get_docker_version=logging_docker,
            has_compose_plugin=logging_compose,
        )
        # All helpers were called despite kernel failure
        assert "kernel" in call_log
        assert "disk" in call_log
        assert "uid" in call_log
        assert "init" in call_log
        assert "docker" in call_log
        assert "compose" in call_log

    def test_port_argument_wires_to_backend_finding(self):
        """custom port=9090 appears in the backend port finding."""
        findings = check_prereqs(**_passing_kwargs(port=9090))
        backend = next(f for f in findings if "backend" in f.name)
        assert "9090" in backend.name

    def test_findings_order(self):
        """kernel, disk, backend-port, 80, 443, root, systemd, docker, compose — in that order."""
        findings = check_prereqs(**_passing_kwargs())
        assert findings[0].name == "kernel version"
        assert findings[1].name == "disk space"
        assert "backend" in findings[2].name
        assert "80" in findings[3].name and "http" in findings[3].name
        assert "443" in findings[4].name and "https" in findings[4].name
        assert findings[5].name == "root access"
        assert findings[6].name == "systemd init"
        assert findings[7].name == "docker engine"
        assert findings[8].name == "docker compose plugin"

    def test_ok_findings_have_empty_remediation(self):
        findings = check_prereqs(**_passing_kwargs())
        for f in findings:
            assert f.remediation == ""

    def test_failing_findings_have_nonempty_remediation(self):
        findings = check_prereqs(
            install_path="/tmp/install",
            read_kernel_release=lambda: "4.14.0",
            get_disk_free_bytes=lambda p: 0,
            is_port_free=lambda port: False,
            get_effective_uid=lambda: 1000,
            read_init_comm=lambda: "init",
            get_docker_version=lambda: None,
            has_compose_plugin=lambda: False,
        )
        for f in findings:
            assert f.remediation != "", f"Expected non-empty remediation for {f.name!r}"


# ── TestDockerCheck ───────────────────────────────────────────────────────────


class TestDockerCheck:
    """Docker Engine prerequisite: present, absent, too old."""

    def _docker_finding(self, version: str | None) -> PrereqFinding:
        findings = check_prereqs(**{**_passing_kwargs(), "get_docker_version": lambda: version})
        return next(f for f in findings if f.name == "docker engine")

    def test_modern_docker_passes(self):
        f = self._docker_finding("27.0.3")
        assert f.ok is True
        assert f.remediation == ""

    def test_exactly_minimum_passes(self):
        f = self._docker_finding("24.0.0")
        assert f.ok is True

    def test_docker_absent_fails(self):
        """None from get_docker_version → not installed / daemon unreachable."""
        f = self._docker_finding(None)
        assert f.ok is False

    def test_too_old_docker_fails(self):
        f = self._docker_finding("23.0.0")
        assert f.ok is False

    def test_fail_message_mentions_install(self):
        f = self._docker_finding(None)
        assert "docker" in f.remediation.lower()

    def test_fail_message_names_old_version(self):
        f = self._docker_finding("20.10.5")
        assert "20.10.5" in f.remediation

    def test_name_field(self):
        f = self._docker_finding("27.0.3")
        assert f.name == "docker engine"


# ── TestComposeCheck ──────────────────────────────────────────────────────────


class TestComposeCheck:
    """Docker Compose v2 plugin prerequisite: present vs absent."""

    def _compose_finding(self, available: bool) -> PrereqFinding:
        findings = check_prereqs(**{**_passing_kwargs(), "has_compose_plugin": lambda: available})
        return next(f for f in findings if f.name == "docker compose plugin")

    def test_compose_present_passes(self):
        f = self._compose_finding(True)
        assert f.ok is True
        assert f.remediation == ""

    def test_compose_absent_fails(self):
        f = self._compose_finding(False)
        assert f.ok is False

    def test_fail_message_mentions_plugin(self):
        f = self._compose_finding(False)
        assert "compose" in f.remediation.lower()

    def test_fail_message_mentions_install(self):
        f = self._compose_finding(False)
        assert "docker-compose-plugin" in f.remediation or "Compose" in f.remediation

    def test_name_field(self):
        f = self._compose_finding(True)
        assert f.name == "docker compose plugin"
