"""installer/tests/test_smoke.py — tests for installer/smoke.py.

Two test tracks per ADR 0015 two-track coverage rule:
1. Orchestration: smoke_test() call-through with DI-mocked predicates.
2. Boundary: each predicate function with mocked run/urlopen/sleep/monotonic.

Coverage:
  P1: systemctl active → pass; failed → P1_FAILED; activating timeout → P1_TIMEOUT
  P2: port bound by correct PID → pass; not bound → P2_NOT_BOUND; wrong PID → P2_WRONG_PID
  P3: /healthz 200+shape → pass; non-200 → P3_FAILED; wrong shape → P3_FAILED
  P4: both probes pass → pass; startup timeout → P4_STARTUP_TIMEOUT; db_ping fail → P4_DB_PING
  P5: / SPA + quickstart → pass; 503 not-built → P5_FRONTEND_NOT_BUILT;
      wrong SPA sig → P5_SPA_WRONG_SIGNATURE; quickstart non-200 → P5_QUICKSTART_FAILED
  Timing: per-predicate budget exercised; 30s total budget exercised.
  smoke_test(): short-circuits on first failure; passes all five on success.
"""

from __future__ import annotations

import json
import time
import urllib.error
from unittest.mock import MagicMock


from installer.smoke import (
    SmokeTestResult,
    _MAX_RETRY_ITERS,
    _QUICKSTART_PATH,
    _check_healthz,
    _check_port_bound,
    _check_spa_and_quickstart,
    _check_startupz_and_readyz,
    _check_systemd_active,
    _parse_ss_port_pid,
    smoke_test,
)
from installer.state import StateFile


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_run_result(stdout: str = "", returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def _make_urlopen_ctx(status: int, body: bytes) -> MagicMock:
    """Return a context-manager mock that yields a response with status+body."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=resp)


def _fast_monotonic(start: float = 0.0):
    """Return a monotonic mock that always says 'now == start' (within budget)."""
    return MagicMock(return_value=start)


def _exhausted_monotonic():
    """Return a monotonic mock where the first call returns 0.0 (the start-time
    anchor consumed by smoke_test's ``start = monotonic()``), then 9999.0
    forever after.  That makes every subsequent _remaining() call return
    budget - (9999 - 0) = deeply negative → budget exhausted on first check.

    Tests that call a predicate *directly* (passing start=0.0 as a parameter)
    must use ``MagicMock(return_value=9999.0)`` instead if they need an
    immediately-exhausted clock and the predicate has no retry loop to guard.
    """
    _calls = [0]

    def _side():
        _calls[0] += 1
        return 0.0 if _calls[0] == 1 else 9999.0

    return MagicMock(side_effect=_side)


def _mock_state(port: int = 8080, data_dir: str = "/data") -> StateFile:
    return StateFile(
        schema_version=1,
        slop_version="5.0.0",
        phase="installed",
        started_at="2026-05-15T00:00:00Z",
        completed_at="2026-05-15T00:01:00Z",
        install_dir="/opt/ms",
        data_dir=data_dir,
        install_user="slop",
        distro="ubuntu",
        distro_version="24.04",
        port=port,
        smoke_test_passed=False,
    )


# ── _parse_ss_port_pid ────────────────────────────────────────────────────────


class TestParseSsPortPid:
    def test_returns_pid_when_port_found(self):
        ss_out = (
            "Netid State   Recv-Q Send-Q  Local Address:Port\n"
            "tcp   LISTEN  0      128     0.0.0.0:8080 0.0.0.0:* "
            'users:(("uvicorn",pid=1234,fd=7))\n'
        )
        assert _parse_ss_port_pid(ss_out, 8080) == "1234"

    def test_returns_none_when_port_not_found(self):
        ss_out = "tcp LISTEN 0 128 0.0.0.0:9090 0.0.0.0:* users:((pid=99))\n"
        assert _parse_ss_port_pid(ss_out, 8080) is None

    def test_returns_empty_string_when_port_found_but_no_pid(self):
        ss_out = "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n"
        assert _parse_ss_port_pid(ss_out, 8080) == ""

    def test_handles_tab_separator(self):
        ss_out = "tcp\tLISTEN\t0\t128\t0.0.0.0:8080\t0.0.0.0:*\tusers:((pid=42))\n"
        assert _parse_ss_port_pid(ss_out, 8080) == "42"


# ── P1: check_systemd_active ──────────────────────────────────────────────────


class TestP1SystemdActive:
    def test_success_active(self):
        run = MagicMock(return_value=_make_run_result("active\n"))
        result = _check_systemd_active(
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert result.passed
        assert result.predicate == "P1"

    def test_failure_failed(self):
        run = MagicMock(return_value=_make_run_result("failed\n"))
        result = _check_systemd_active(
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P1_FAILED"
        assert "failed to start" in result.operator_message
        assert "journalctl" in result.diagnostic_command

    def test_failure_activating_past_retry_budget(self):
        # monotonic always returns 9999 → retry_deadline is in the past
        run = MagicMock(return_value=_make_run_result("activating\n"))
        result = _check_systemd_active(
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=_exhausted_monotonic(),
        )
        assert not result.passed
        assert result.failure_shape == "P1_TIMEOUT"
        assert "10 seconds" in result.operator_message
        assert "systemctl status" in result.diagnostic_command

    def test_initial_settle_sleep_called(self):
        run = MagicMock(return_value=_make_run_result("active\n"))
        sleep = MagicMock()
        _check_systemd_active(0.0, 30.0, run=run, sleep=sleep, monotonic=_fast_monotonic(0.0))
        assert sleep.call_count >= 1
        first_sleep_arg = sleep.call_args_list[0][0][0]
        assert 0.0 < first_sleep_arg <= 2.0

    def test_retry_on_inactive(self):
        # Return inactive twice then active.
        results = [
            _make_run_result("inactive\n"),
            _make_run_result("inactive\n"),
            _make_run_result("active\n"),
        ]
        run = MagicMock(side_effect=results)
        times = [0.0, 0.0, 0.5, 0.5, 1.0, 1.0, 1.5, 1.5]
        monotonic = MagicMock(side_effect=times)
        result = _check_systemd_active(0.0, 30.0, run=run, sleep=MagicMock(), monotonic=monotonic)
        assert result.passed
        assert run.call_count == 3

    def test_uses_custom_unit_name(self):
        run = MagicMock(return_value=_make_run_result("active\n"))
        _check_systemd_active(
            0.0,
            30.0,
            unit="custom.service",
            run=run,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        called_cmd = run.call_args[0][0]
        assert "custom.service" in called_cmd

    def test_total_budget_caps_settle(self):
        # With very little budget remaining, settle should be near zero.
        run = MagicMock(return_value=_make_run_result("active\n"))
        sleep = MagicMock()
        # start=0.0, budget_s=1.0, monotonic returns 0.8 (only 0.2s left)
        monotonic = MagicMock(return_value=0.8)
        _check_systemd_active(0.0, 1.0, run=run, sleep=sleep, monotonic=monotonic)
        if sleep.call_count > 0:
            assert sleep.call_args_list[0][0][0] <= 0.2 + 1e-9


# ── P2: check_port_bound ──────────────────────────────────────────────────────


class TestP2PortBound:
    _SS_WITH_PID = "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((pid=1234))\n"
    _SS_WRONG_PID = "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((pid=9999))\n"
    _SS_NO_PORT = "tcp LISTEN 0 128 0.0.0.0:9090 0.0.0.0:* users:((pid=1234))\n"

    def _run_factory(self, main_pid: str, ss_out: str):
        def run(cmd, **kw):
            if "MainPID" in cmd:
                return _make_run_result(main_pid + "\n")
            return _make_run_result(ss_out)

        return run

    def test_success_port_bound_correct_pid(self):
        run = self._run_factory("1234", self._SS_WITH_PID)
        result = _check_port_bound(
            8080,
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert result.passed

    def test_failure_port_not_bound(self):
        run = self._run_factory("1234", self._SS_NO_PORT)
        result = _check_port_bound(
            8080,
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=_exhausted_monotonic(),
        )
        assert not result.passed
        assert result.failure_shape == "P2_NOT_BOUND"
        assert "did not bind" in result.operator_message
        assert "ss -ltnp" in result.diagnostic_command

    def test_failure_port_bound_wrong_pid(self):
        run = self._run_factory("1234", self._SS_WRONG_PID)
        result = _check_port_bound(
            8080,
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P2_WRONG_PID"
        assert "not by the slop process" in result.operator_message
        assert "lsof" in result.diagnostic_command

    def test_uses_port_number_in_messages(self):
        # Port 8080 is not in _SS_NO_PORT (which has port 9090) → P2_NOT_BOUND.
        # Use always-9999 monotonic so budget is exhausted before any loop
        # iteration and run() is never called (avoids _SS_NO_PORT confusion).
        run = self._run_factory("1234", self._SS_NO_PORT)
        result = _check_port_bound(
            8080,
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=MagicMock(return_value=9999.0),
        )
        assert not result.passed
        assert "8080" in result.operator_message

    def test_retries_until_budget_exhausted(self):
        # Wrap _run_factory in MagicMock so call_count is trackable.
        _run = self._run_factory("1234", self._SS_NO_PORT)
        run = MagicMock(side_effect=_run)
        sleep = MagicMock()
        times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]
        monotonic = MagicMock(side_effect=times)
        result = _check_port_bound(8080, 0.0, 30.0, run=run, sleep=sleep, monotonic=monotonic)
        assert not result.passed
        assert run.call_count >= 2


# ── P3: check_healthz ─────────────────────────────────────────────────────────


class TestP3Healthz:
    _GOOD_BODY = json.dumps({"status": "ok", "ts": 1234567890}).encode()
    _BAD_STATUS_BODY = json.dumps({"status": "fail"}).encode()
    _NO_TS_BODY = json.dumps({"status": "ok"}).encode()
    _BAD_JSON = b"not-json"

    def test_success_200_valid_shape(self):
        urlopen = _make_urlopen_ctx(200, self._GOOD_BODY)
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert result.passed

    def test_failure_non200(self):
        urlopen = _make_urlopen_ctx(503, b"")
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.predicate == "P3"
        assert "did not respond as expected" in result.operator_message

    def test_failure_200_wrong_shape_no_status(self):
        body = json.dumps({"ts": 123}).encode()
        urlopen = _make_urlopen_ctx(200, body)
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert "did not respond as expected" in result.operator_message

    def test_failure_200_wrong_shape_bad_status_value(self):
        urlopen = _make_urlopen_ctx(200, self._BAD_STATUS_BODY)
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed

    def test_failure_200_wrong_shape_no_ts(self):
        urlopen = _make_urlopen_ctx(200, self._NO_TS_BODY)
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed

    def test_failure_connection_error_then_success(self):
        # First call raises URLError, second succeeds.
        good_resp = MagicMock()
        good_resp.status = 200
        good_resp.read.return_value = self._GOOD_BODY
        good_resp.__enter__ = MagicMock(return_value=good_resp)
        good_resp.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise urllib.error.URLError("connection refused")
            return good_resp

        times = [0.0, 0.0, 0.0, 0.1, 0.1, 0.1, 0.2, 0.2, 0.2]
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=fake_urlopen,
            sleep=MagicMock(),
            monotonic=MagicMock(side_effect=times),
        )
        assert result.passed
        assert call_count[0] == 2

    def test_budget_exhaustion_returns_failure(self):
        urlopen = MagicMock(side_effect=urllib.error.URLError("refused"))
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_exhausted_monotonic(),
        )
        assert not result.passed
        assert result.failure_shape == "P3_FAILED"

    def test_diagnostic_command_references_journalctl(self):
        urlopen = _make_urlopen_ctx(503, b"")
        result = _check_healthz(
            8080,
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert "journalctl" in result.diagnostic_command


# ── P4: check_startupz_and_readyz ─────────────────────────────────────────────


class TestP4StartupzReadyz:
    _STARTUPZ_OK = json.dumps({"status": "ok", "startup_complete": True}).encode()
    _STARTUPZ_STARTING = json.dumps({"status": "starting", "startup_complete": False}).encode()
    _READYZ_OK = json.dumps(
        {"status": "ok", "checks": {"db_ping": "ok", "state_configured": "ok"}}
    ).encode()
    _READYZ_FAIL = json.dumps(
        {"status": "not_ready", "checks": {"db_ping": "fail: OperationalError"}}
    ).encode()

    def _make_dual_urlopen(self, startupz_body, startupz_status, readyz_body, readyz_status):
        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            if "/startupz" in req.full_url:
                resp.status = startupz_status
                resp.read.return_value = startupz_body
            else:
                resp.status = readyz_status
                resp.read.return_value = readyz_body
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        return fake_urlopen

    def test_success_both_probes_pass(self):
        urlopen = self._make_dual_urlopen(self._STARTUPZ_OK, 200, self._READYZ_OK, 200)
        result = _check_startupz_and_readyz(
            8080,
            "/data",
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert result.passed
        assert result.predicate == "P4"

    def test_failure_startupz_never_completes(self):
        urlopen = self._make_dual_urlopen(self._STARTUPZ_STARTING, 503, self._READYZ_OK, 200)
        result = _check_startupz_and_readyz(
            8080,
            "/data",
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_exhausted_monotonic(),
        )
        assert not result.passed
        assert result.failure_shape == "P4_STARTUP_TIMEOUT"
        assert "startup did not complete" in result.operator_message
        assert "journalctl" in result.diagnostic_command

    def test_failure_readyz_db_ping_not_ok(self):
        urlopen = self._make_dual_urlopen(self._STARTUPZ_OK, 200, self._READYZ_FAIL, 503)
        result = _check_startupz_and_readyz(
            8080,
            "/data",
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P4_DB_PING"
        assert "cannot reach its database" in result.operator_message
        assert "/data" in result.operator_message

    def test_failure_message_includes_data_dir_path(self):
        urlopen = self._make_dual_urlopen(self._STARTUPZ_OK, 200, self._READYZ_FAIL, 503)
        result = _check_startupz_and_readyz(
            8080,
            "/var/mydata",
            0.0,
            30.0,
            urlopen=urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert "/var/mydata" in result.operator_message

    def test_retries_when_startup_not_complete(self):
        call_count = [0]
        startupz_bodies = [self._STARTUPZ_STARTING, self._STARTUPZ_STARTING, self._STARTUPZ_OK]

        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            if "/startupz" in req.full_url:
                resp.status = 200
                resp.read.return_value = startupz_bodies[min(call_count[0], 2)]
                call_count[0] += 1
            else:
                resp.status = 200
                resp.read.return_value = self._READYZ_OK
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        times = [0.0, 0.0, 0.5, 0.5, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0, 2.5]
        result = _check_startupz_and_readyz(
            8080,
            "/data",
            0.0,
            30.0,
            urlopen=fake_urlopen,
            sleep=MagicMock(),
            monotonic=MagicMock(side_effect=times),
        )
        assert result.passed
        assert call_count[0] >= 3


# ── P5: check_spa_and_quickstart ──────────────────────────────────────────────


class TestP5SpaAndQuickstart:
    _GOOD_SPA = (
        b"<!DOCTYPE html><html><head><title>SLOP</title></head>"
        b'<body><div id="app"></div>'
        b'<script src="/assets/index-abc123.js"></script></body></html>'
    )
    _FRONTEND_NOT_BUILT = json.dumps(
        {"detail": "Frontend not built. Run: cd frontend && npm run build"}
    ).encode()
    _QS_OK = json.dumps({"show": True, "phases": []}).encode()

    def _make_dual_urlopen(self, spa_status, spa_body, qs_status, qs_body):
        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            if req.full_url.endswith("/"):
                resp.status = spa_status
                resp.read.return_value = spa_body
            else:
                resp.status = qs_status
                resp.read.return_value = qs_body
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        return fake_urlopen

    def test_success_spa_and_quickstart(self):
        urlopen = self._make_dual_urlopen(200, self._GOOD_SPA, 200, self._QS_OK)
        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=urlopen,
            monotonic=_fast_monotonic(0.0),
        )
        assert result.passed
        assert result.predicate == "P5"

    def test_failure_503_frontend_not_built(self):
        urlopen = self._make_dual_urlopen(503, self._FRONTEND_NOT_BUILT, 200, self._QS_OK)
        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=urlopen,
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P5_FRONTEND_NOT_BUILT"
        assert "frontend was not built" in result.operator_message
        assert "/opt/ms" in result.diagnostic_command

    def test_failure_200_wrong_spa_signature(self):
        bad_spa = b'<html><body><div id="notapp"></div></body></html>'
        urlopen = self._make_dual_urlopen(200, bad_spa, 200, self._QS_OK)
        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=urlopen,
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P5_SPA_WRONG_SIGNATURE"
        assert "SPA signature" in result.operator_message

    def test_failure_quickstart_non200(self):
        urlopen = self._make_dual_urlopen(200, self._GOOD_SPA, 404, b"Not Found")
        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=urlopen,
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P5_QUICKSTART_FAILED"
        assert "QuickStart API" in result.operator_message
        assert _QUICKSTART_PATH in result.diagnostic_command

    def test_failure_quickstart_not_json(self):
        urlopen = self._make_dual_urlopen(200, self._GOOD_SPA, 200, b"not-json")
        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=urlopen,
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.failure_shape == "P5_QUICKSTART_FAILED"

    def test_failure_spa_unreachable(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=fake_urlopen,
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed

    def test_failure_budget_exhausted_before_p5(self):
        urlopen = self._make_dual_urlopen(200, self._GOOD_SPA, 200, self._QS_OK)
        # P5 has no retry loop so _exhausted_monotonic's first-call-0.0 would
        # make _remaining() return 30.0 (not exhausted) and the test would pass
        # rather than return P5_BUDGET_EXHAUSTED.  Use a plain constant mock.
        result = _check_spa_and_quickstart(
            8080,
            "/opt/ms",
            0.0,
            30.0,
            urlopen=urlopen,
            monotonic=MagicMock(return_value=9999.0),
        )
        assert not result.passed
        assert result.failure_shape == "P5_BUDGET_EXHAUSTED"


# ── smoke_test() orchestration ────────────────────────────────────────────────


class TestSmokeTestOrchestration:
    """Verify smoke_test() delegates to predicates and short-circuits correctly.

    Uses DI-mocked predicate functions to isolate orchestration logic.
    """

    def _good_state(self) -> StateFile:
        return _mock_state(port=8080, data_dir="/data")

    def _make_pipeline_mocks(
        self,
        p1_pass=True,
        p2_pass=True,
        p3_pass=True,
        p4_pass=True,
        p5_pass=True,
    ):
        def _make_pred(predicate_name: str, passed: bool):
            return SmokeTestResult(
                predicate=predicate_name,
                passed=passed,
                failure_shape="" if passed else f"{predicate_name}_FAIL",
                operator_message="" if passed else f"{predicate_name} failed",
                diagnostic_command="",
            )

        return {
            "p1": MagicMock(return_value=_make_pred("P1", p1_pass)),
            "p2": MagicMock(return_value=_make_pred("P2", p2_pass)),
            "p3": MagicMock(return_value=_make_pred("P3", p3_pass)),
            "p4": MagicMock(return_value=_make_pred("P4", p4_pass)),
            "p5": MagicMock(return_value=_make_pred("P5", p5_pass)),
        }

    def test_all_pass_returns_success(self, tmp_path):
        state_read = MagicMock(return_value=self._good_state())

        # P1+P2: run returns appropriate output per command.
        _SS_OUT = "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((pid=1234))\n"

        def _run(cmd, **kw):
            if "is-active" in cmd:
                return _make_run_result("active\n")
            if "MainPID" in cmd:
                return _make_run_result("1234\n")
            return _make_run_result(_SS_OUT)

        # P3/P4/P5: urlopen returns appropriate bodies per URL.
        _P3 = json.dumps({"status": "ok", "ts": 123}).encode()
        _STARTUP = json.dumps({"status": "ok", "startup_complete": True}).encode()
        _READYZ = json.dumps({"status": "ok", "checks": {"db_ping": "ok"}}).encode()
        _SPA = (
            b"<html><head><title>SLOP</title></head>"
            b'<body><div id="app"></div>'
            b'<script src="/assets/index-abc.js"></script></body></html>'
        )
        _QS = json.dumps({"show": False, "phases": []}).encode()

        def _urlopen(req, timeout=None):
            url = req.full_url
            resp = MagicMock()
            resp.status = 200
            if "/healthz" in url:
                resp.read.return_value = _P3
            elif "/startupz" in url:
                resp.read.return_value = _STARTUP
            elif "/readyz" in url:
                resp.read.return_value = _READYZ
            elif "/quickstart" in url:
                resp.read.return_value = _QS
            else:
                resp.read.return_value = _SPA
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        result = smoke_test(
            tmp_path,
            state_read=state_read,
            run=_run,
            urlopen=_urlopen,
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert result.passed
        assert result.predicate == "all"

    def test_p1_failure_short_circuits(self, tmp_path):
        state_read = MagicMock(return_value=self._good_state())
        run = MagicMock(return_value=_make_run_result("failed\n"))
        result = smoke_test(
            tmp_path,
            state_read=state_read,
            run=run,
            urlopen=MagicMock(),
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        assert not result.passed
        assert result.predicate == "P1"

    def test_reads_port_from_state_file(self, tmp_path):
        state_read = MagicMock(return_value=_mock_state(port=9090))
        # P1 fails so it short-circuits before P2's retry loop.
        # state_read is called once before any predicate, so the assertion holds.
        run = MagicMock(return_value=_make_run_result("failed\n"))
        smoke_test(
            tmp_path,
            state_read=state_read,
            run=run,
            urlopen=MagicMock(side_effect=urllib.error.URLError("x")),
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        state_read.assert_called_once()

    def test_state_none_uses_default_port(self, tmp_path):
        state_read = MagicMock(return_value=None)
        run = MagicMock(return_value=_make_run_result("failed\n"))
        result = smoke_test(
            tmp_path,
            state_read=state_read,
            run=run,
            urlopen=MagicMock(),
            sleep=MagicMock(),
            monotonic=_fast_monotonic(0.0),
        )
        # Smoke starts (P1 fails with "failed")
        assert not result.passed

    def test_30s_total_budget_exercised(self, tmp_path):
        # With exhausted monotonic, P1 settle is 0s and retry_deadline is past → P1_TIMEOUT.
        state_read = MagicMock(return_value=self._good_state())
        run = MagicMock(return_value=_make_run_result("activating\n"))
        result = smoke_test(
            tmp_path,
            timeout_budget_s=30.0,
            state_read=state_read,
            run=run,
            urlopen=MagicMock(),
            sleep=MagicMock(),
            monotonic=_exhausted_monotonic(),
        )
        # With exhausted monotonic, P1 returns P1_TIMEOUT immediately.
        assert not result.passed
        assert result.predicate == "P1"
        assert result.failure_shape == "P1_TIMEOUT"


# ── TestMaxIterationGuard ─────────────────────────────────────────────────────


class TestMaxIterationGuard:
    """Regression: retry while-loops cap at _MAX_RETRY_ITERS iterations.

    A constant-returning monotonic (simulating a frozen or broken clock) would
    cause the while condition to be True forever without the guard.  Each test
    verifies that the predicate returns the correct timeout result within a
    bounded number of iterations instead of hanging.
    """

    def test_p1_broken_monotonic_terminates(self):
        # monotonic always returns 5.0: retry_deadline = 5.0 + 10.0 = 15.0
        # → while 5.0 < 15.0 is always True without the guard.
        broken = MagicMock(return_value=5.0)
        run = MagicMock(return_value=_make_run_result("activating\n"))
        t0 = time.monotonic()
        result = _check_systemd_active(
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=broken,
        )
        elapsed = time.monotonic() - t0
        assert not result.passed
        assert result.failure_shape == "P1_TIMEOUT"
        assert run.call_count <= _MAX_RETRY_ITERS
        assert elapsed < 1.0, f"P1 guard took {elapsed:.3f}s; should be sub-second"

    def test_p2_broken_monotonic_terminates(self):
        # monotonic always returns 5.0: retry_deadline = 5.0 + 5.0 = 10.0
        # → while 5.0 < 10.0 is always True without the guard.
        broken = MagicMock(return_value=5.0)

        def run(cmd, **kw):
            if "MainPID" in cmd:
                return _make_run_result("1234\n")
            return _make_run_result("")  # ss: port not bound

        t0 = time.monotonic()
        result = _check_port_bound(
            8080,
            0.0,
            30.0,
            run=run,
            sleep=MagicMock(),
            monotonic=broken,
        )
        elapsed = time.monotonic() - t0
        assert not result.passed
        assert result.failure_shape == "P2_NOT_BOUND"
        assert elapsed < 1.0, f"P2 guard took {elapsed:.3f}s; should be sub-second"
