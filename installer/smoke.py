"""installer/smoke.py — ADR 0015 first-run readiness smoke test.

Five predicates (P1-P5) evaluated in order per §1. Short-circuits on first
failure. Returns a SmokeTestResult on both success and failure.

P5 QuickStart endpoint: GET /api/v1/quickstart
Rationale: only non-mutating GET in the QuickStart router; no auth state
required on a fresh install; returns HTTP 200 + application/json with all
phases pending when the database is empty; safe to call before the wizard
is touched.

All subprocess calls use installer._run.run_required (Core Rule 5.27).
All HTTP calls use urllib.request (stdlib); no external dependencies.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from installer._run import MissingBinaryError, run_required
from installer.state import STATE_FILE_NAME, read_state_file

# P5: QuickStart endpoint URL — see module docstring for rationale.
_QUICKSTART_PATH = "/api/v1/quickstart"

# P5: SPA signature patterns (three-part match per ADR 0015 §3).
_SPA_RE_MOUNT = re.compile(r'id=["\']app["\']')
_SPA_RE_ASSET = re.compile(r"/assets/index-")
_SPA_RE_TITLE = re.compile(r"slop", re.IGNORECASE)

# INV-8: unfilled placeholder regex.
_UNFILLED_RE = re.compile(r"<[a-z_]+>")

# Backoff delays for P3/P4 (seconds between retry attempts).
_BACKOFF_DELAYS = [0.5, 1.0, 2.0, 4.0]

# Safety cap: exit any retry while-loop after this many iterations even if the
# monotonic clock appears frozen (broken mock or pathological real clock).
_MAX_RETRY_ITERS = 100


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class SmokeTestResult:
    """Result of smoke_test() or an individual predicate check."""

    predicate: str  # "P1" .. "P5", or "all" on overall success
    passed: bool
    failure_shape: str  # short code, e.g. "P1_FAILED"; empty on pass
    operator_message: str  # human-readable message per ADR 0015 §4
    diagnostic_command: str  # suggested command for operator; empty on pass


# ── Internal helpers ──────────────────────────────────────────────────────────


def _remaining(start: float, budget_s: float, monotonic: Callable) -> float:
    """Seconds remaining in the total budget (may be negative when exhausted)."""
    return budget_s - (monotonic() - start)


def _http_get(
    url: str,
    timeout_s: float,
    urlopen: Callable,
) -> tuple[int, bytes]:
    """Execute a GET request. Returns (status_code, body_bytes).

    Raises urllib.error.URLError, http.client.HTTPException, socket.timeout,
    or OSError on connection-level failures.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "slop-smoke/5"})  # noqa: S310 — installer fetch of a known/operator-configured URL over urllib; single-trusted-operator threat model
    with urlopen(req, timeout=timeout_s) as resp:
        return resp.status, resp.read()


# ── P1: systemd unit active ───────────────────────────────────────────────────


def _check_systemd_active(
    start: float,
    budget_s: float,
    *,
    unit: str = "slop.service",
    run: Callable = run_required,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeTestResult:
    """P1: systemctl is-active reports 'active'.

    2s initial settle, then retry every 500ms up to 10s total.
    """
    # Initial 2s settle — capped by remaining total budget.
    settle = min(2.0, max(0.0, _remaining(start, budget_s, monotonic)))
    if settle > 0.0:
        sleep(settle)

    p1_budget = min(10.0, _remaining(start, budget_s, monotonic))
    retry_deadline = monotonic() + max(0.0, p1_budget)

    _iters = 0
    while monotonic() < retry_deadline:
        _iters += 1
        if _iters > _MAX_RETRY_ITERS:
            break
        try:
            result = run(["systemctl", "is-active", unit], timeout=1.0)
            status_str = result.stdout.strip()
        except (MissingBinaryError, Exception):
            status_str = "error"

        if status_str == "active":
            return SmokeTestResult(
                predicate="P1",
                passed=True,
                failure_shape="",
                operator_message="",
                diagnostic_command="",
            )
        if status_str == "failed":
            return SmokeTestResult(
                predicate="P1",
                passed=False,
                failure_shape="P1_FAILED",
                operator_message=(
                    "The slop service failed to start. Recent backend logs may show why."
                ),
                diagnostic_command="journalctl -u slop.service -n 50 --no-pager",
            )

        remaining = retry_deadline - monotonic()
        if remaining > 0.0:
            sleep(min(0.5, remaining))

    return SmokeTestResult(
        predicate="P1",
        passed=False,
        failure_shape="P1_TIMEOUT",
        operator_message=(
            "The slop service did not reach `active` state within 10 seconds. "
            "The unit may be slow to start or stuck in a startup hook."
        ),
        diagnostic_command="systemctl status slop.service",
    )


# ── P2: port bound by slop process ─────────────────────────────────────


def _parse_ss_port_pid(ss_output: str, port: int) -> str | None:
    """Return PID string bound to *port* in ss -ltnp output, or None.

    Returns "" (empty string) if port is found but no PID is visible.
    Returns None if port is not in LISTEN state at all.
    """
    port_re = re.compile(rf":{re.escape(str(port))}[ \t]")
    pid_re = re.compile(r"pid=(\d+)")
    for line in ss_output.splitlines():
        if port_re.search(line):
            m = pid_re.search(line)
            return m.group(1) if m else ""
    return None


def _check_port_bound(
    port: int,
    start: float,
    budget_s: float,
    *,
    unit: str = "slop.service",
    run: Callable = run_required,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeTestResult:
    """P2: port is bound on 127.0.0.1 by the slop process.

    Retry every 500ms up to 5s total.
    """
    p2_budget = min(5.0, _remaining(start, budget_s, monotonic))
    retry_deadline = monotonic() + max(0.0, p2_budget)

    _iters = 0
    while monotonic() < retry_deadline:
        _iters += 1
        if _iters > _MAX_RETRY_ITERS:
            break
        try:
            pid_result = run(
                ["systemctl", "show", "-p", "MainPID", "--value", unit],
                timeout=1.0,
            )
            main_pid = pid_result.stdout.strip()
            ss_result = run(["ss", "-ltnp"], timeout=1.0)
            bound_pid = _parse_ss_port_pid(ss_result.stdout, port)
        except (MissingBinaryError, Exception):
            bound_pid = None
            main_pid = ""

        if bound_pid is not None:
            # Port is bound — check PID ownership.
            if main_pid and main_pid != "0" and bound_pid == main_pid:
                return SmokeTestResult(
                    predicate="P2",
                    passed=True,
                    failure_shape="",
                    operator_message="",
                    diagnostic_command="",
                )
            if bound_pid != main_pid:
                return SmokeTestResult(
                    predicate="P2",
                    passed=False,
                    failure_shape="P2_WRONG_PID",
                    operator_message=(
                        f"Port {port} is bound, but not by the slop process. "
                        "Another service may be using the port."
                    ),
                    diagnostic_command=f"ss -ltnp && lsof -i :{port}",
                )

        remaining = retry_deadline - monotonic()
        if remaining > 0.0:
            sleep(min(0.5, remaining))

    return SmokeTestResult(
        predicate="P2",
        passed=False,
        failure_shape="P2_NOT_BOUND",
        operator_message=(
            f"The slop service is active but did not bind to port {port} "
            "within 5 seconds. The process may be initializing or another service "
            "may have the port."
        ),
        diagnostic_command="ss -ltnp && systemctl status slop.service",
    )


# ── P3: /healthz ─────────────────────────────────────────────────────────────


def _check_healthz(
    port: int,
    start: float,
    budget_s: float,
    *,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeTestResult:
    """P3: GET /healthz returns 200 with {status: "ok", ts: <number>}.

    Retry with backoff (500ms, 1s, 2s, 4s) up to 10s total.
    """
    url = f"http://127.0.0.1:{port}/healthz"
    fail_result = SmokeTestResult(
        predicate="P3",
        passed=False,
        failure_shape="P3_FAILED",
        operator_message=(
            "The slop backend is running but `/healthz` did not respond "
            "as expected. The FastAPI app may have failed to install routes."
        ),
        diagnostic_command="journalctl -u slop.service -n 50 --no-pager",
    )

    p3_budget = min(10.0, _remaining(start, budget_s, monotonic))
    retry_deadline = monotonic() + max(0.0, p3_budget)

    for _n, delay in enumerate([0.0, *list(_BACKOFF_DELAYS)]):
        if _n >= _MAX_RETRY_ITERS:
            return fail_result
        if monotonic() >= retry_deadline:
            return fail_result
        if delay > 0.0:
            remaining = retry_deadline - monotonic()
            if remaining <= 0.0:
                return fail_result
            sleep(min(delay, remaining))

        try:
            status, body = _http_get(url, 3.0, urlopen)
        except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError):
            continue

        if status != 200:
            continue

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return fail_result

        if not isinstance(data.get("status"), str) or data["status"] != "ok":
            return fail_result
        ts = data.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return fail_result

        return SmokeTestResult(
            predicate="P3",
            passed=True,
            failure_shape="",
            operator_message="",
            diagnostic_command="",
        )

    return fail_result


# ── P4: /startupz and /readyz ─────────────────────────────────────────────────


def _check_startupz_and_readyz(
    port: int,
    data_dir: str,
    start: float,
    budget_s: float,
    *,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeTestResult:
    """P4: /startupz has startup_complete:true AND /readyz has db_ping:ok.

    Joint retry with backoff (500ms, 1s, 2s, 4s) up to 10s total.
    """
    startupz_url = f"http://127.0.0.1:{port}/startupz"
    readyz_url = f"http://127.0.0.1:{port}/readyz"

    p4_budget = min(10.0, _remaining(start, budget_s, monotonic))
    retry_deadline = monotonic() + max(0.0, p4_budget)

    for _n, delay in enumerate([0.0, *list(_BACKOFF_DELAYS)]):
        if _n >= _MAX_RETRY_ITERS:
            break
        if monotonic() >= retry_deadline:
            break
        if delay > 0.0:
            remaining = retry_deadline - monotonic()
            if remaining <= 0.0:
                break
            sleep(min(delay, remaining))

        # Check /startupz
        try:
            _sz_status, sz_body = _http_get(startupz_url, 3.0, urlopen)
        except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError):
            continue

        try:
            sz_data = json.loads(sz_body)
        except json.JSONDecodeError:
            continue

        startup_complete = sz_data.get("startup_complete") is True

        if not startup_complete:
            # Still starting — retry
            continue

        # /startupz passed — check /readyz
        try:
            _rz_status, rz_body = _http_get(readyz_url, 3.0, urlopen)
        except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError):
            continue

        try:
            rz_data = json.loads(rz_body)
        except json.JSONDecodeError:
            continue

        checks = rz_data.get("checks", {})
        db_ping = checks.get("db_ping", "")

        if db_ping == "ok":
            return SmokeTestResult(
                predicate="P4",
                passed=True,
                failure_shape="",
                operator_message="",
                diagnostic_command="",
            )

        if db_ping != "ok":
            # db_ping failed — return immediately (structural failure)
            return SmokeTestResult(
                predicate="P4",
                passed=False,
                failure_shape="P4_DB_PING",
                operator_message=(
                    f"The slop backend cannot reach its database at "
                    f"{data_dir}/state.db. The data directory may have wrong "
                    "ownership or permissions."
                ),
                diagnostic_command=(
                    f"ls -la {data_dir}/ && journalctl -u slop.service -n 50 --no-pager"
                ),
            )

    # Retry budget exhausted — startup did not complete
    return SmokeTestResult(
        predicate="P4",
        passed=False,
        failure_shape="P4_STARTUP_TIMEOUT",
        operator_message=(
            "The slop backend's startup did not complete within 10 seconds. "
            "The lifespan handler may be slow or stuck (commonly: database "
            "initialization, scheduler startup)."
        ),
        diagnostic_command="journalctl -u slop.service -n 100 --no-pager",
    )


# ── P5: SPA + QuickStart ──────────────────────────────────────────────────────


def _check_spa_and_quickstart(
    port: int,
    install_dir: str,
    start: float,
    budget_s: float,
    *,
    urlopen: Callable = urllib.request.urlopen,
    monotonic: Callable[[], float] = time.monotonic,
) -> SmokeTestResult:
    """P5: / returns SPA and QuickStart GET returns 200+JSON.

    One attempt per endpoint; no retry (structural failures per §2).
    """
    base = f"http://127.0.0.1:{port}"

    # Check remaining budget before P5.
    if _remaining(start, budget_s, monotonic) <= 0.0:
        return SmokeTestResult(
            predicate="P5",
            passed=False,
            failure_shape="P5_BUDGET_EXHAUSTED",
            operator_message=("The smoke test budget was exhausted before P5 could run."),
            diagnostic_command=f"curl -s -i http://127.0.0.1:{port}/",
        )

    # 1. Check / (SPA)
    try:
        spa_status, spa_body = _http_get(f"{base}/", 3.0, urlopen)
    except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError):
        return SmokeTestResult(
            predicate="P5",
            passed=False,
            failure_shape="P5_SPA_UNREACHABLE",
            operator_message=(
                "The frontend is serving but does not match the expected "
                "slop SPA signature. This may indicate a misconfigured "
                "reverse proxy fronting the port, or a corrupted build."
            ),
            diagnostic_command=f"curl -s http://127.0.0.1:{port}/ | head -50",
        )

    if spa_status == 503:
        try:
            detail = json.loads(spa_body).get("detail", "")
            if "Frontend not built" in str(detail):
                return SmokeTestResult(
                    predicate="P5",
                    passed=False,
                    failure_shape="P5_FRONTEND_NOT_BUILT",
                    operator_message=(
                        "The frontend was not built during install. "
                        "The install pipeline's frontend build step may have "
                        "failed silently."
                    ),
                    diagnostic_command=f"ls {install_dir}/frontend/dist/",
                )
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return SmokeTestResult(
            predicate="P5",
            passed=False,
            failure_shape="P5_SPA_503",
            operator_message=(
                "The frontend is serving but does not match the expected "
                "slop SPA signature. This may indicate a misconfigured "
                "reverse proxy fronting the port, or a corrupted build."
            ),
            diagnostic_command=f"curl -s http://127.0.0.1:{port}/ | head -50",
        )

    if spa_status != 200:
        return SmokeTestResult(
            predicate="P5",
            passed=False,
            failure_shape="P5_SPA_NON200",
            operator_message=(
                "The frontend is serving but does not match the expected "
                "slop SPA signature. This may indicate a misconfigured "
                "reverse proxy fronting the port, or a corrupted build."
            ),
            diagnostic_command=f"curl -s http://127.0.0.1:{port}/ | head -50",
        )

    # Validate three-part SPA signature per ADR 0015 §3.
    body_text = spa_body.decode("utf-8", errors="replace")
    if (
        not _SPA_RE_MOUNT.search(body_text)
        or not _SPA_RE_ASSET.search(body_text)
        or not _SPA_RE_TITLE.search(body_text)
    ):
        return SmokeTestResult(
            predicate="P5",
            passed=False,
            failure_shape="P5_SPA_WRONG_SIGNATURE",
            operator_message=(
                "The frontend is serving but does not match the expected "
                "slop SPA signature. This may indicate a misconfigured "
                "reverse proxy fronting the port, or a corrupted build."
            ),
            diagnostic_command=f"curl -s http://127.0.0.1:{port}/ | head -50",
        )

    # 2. Check QuickStart endpoint.
    qs_fail = SmokeTestResult(
        predicate="P5",
        passed=False,
        failure_shape="P5_QUICKSTART_FAILED",
        operator_message=(
            "The QuickStart API endpoint did not respond as expected. "
            "The QuickStart router may not be mounted."
        ),
        diagnostic_command=(f"curl -s -i http://127.0.0.1:{port}{_QUICKSTART_PATH}"),
    )

    try:
        qs_status, qs_body = _http_get(f"{base}{_QUICKSTART_PATH}", 3.0, urlopen)
    except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError):
        return qs_fail

    if qs_status != 200:
        return qs_fail

    try:
        json.loads(qs_body)
    except json.JSONDecodeError:
        return qs_fail

    return SmokeTestResult(
        predicate="P5",
        passed=True,
        failure_shape="",
        operator_message="",
        diagnostic_command="",
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def smoke_test(
    install_dir: str | Path,
    *,
    data_dir: str | None = None,
    timeout_budget_s: float = 30.0,
    run: Callable = run_required,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    state_read: Callable = read_state_file,
) -> SmokeTestResult:
    """Run the five ADR 0015 readiness predicates in order.

    Short-circuits on first failure. Returns the first failing SmokeTestResult,
    or a passing SmokeTestResult with predicate="all" on overall success.

    The total wall-clock budget is timeout_budget_s (default 30s). Each
    predicate's retry budget is min(predicate_max, remaining_total).

    Args:
        install_dir: Path to the install directory (contains .installer-state.json).
        data_dir: Override for the data directory path; read from state file if None.
        timeout_budget_s: Total wall-clock budget in seconds (default 30s).
        run: Injectable subprocess wrapper (default: run_required).
        urlopen: Injectable HTTP function (default: urllib.request.urlopen).
        sleep: Injectable sleep function (default: time.sleep).
        monotonic: Injectable monotonic clock (default: time.monotonic).
        state_read: Injectable state-file reader (default: read_state_file).
    """
    install_dir_path = Path(install_dir)
    state = state_read(install_dir_path / STATE_FILE_NAME)
    port = state.port if state is not None else 8080
    _data_dir = data_dir if data_dir is not None else (state.data_dir if state is not None else "")

    start = monotonic()

    r = _check_systemd_active(
        start,
        timeout_budget_s,
        run=run,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not r.passed:
        return r

    r = _check_port_bound(
        port,
        start,
        timeout_budget_s,
        run=run,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not r.passed:
        return r

    r = _check_healthz(
        port,
        start,
        timeout_budget_s,
        urlopen=urlopen,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not r.passed:
        return r

    r = _check_startupz_and_readyz(
        port,
        _data_dir,
        start,
        timeout_budget_s,
        urlopen=urlopen,
        sleep=sleep,
        monotonic=monotonic,
    )
    if not r.passed:
        return r

    r = _check_spa_and_quickstart(
        port,
        str(install_dir_path),
        start,
        timeout_budget_s,
        urlopen=urlopen,
        monotonic=monotonic,
    )
    if not r.passed:
        return r

    return SmokeTestResult(
        predicate="all",
        passed=True,
        failure_shape="",
        operator_message="",
        diagnostic_command="",
    )
