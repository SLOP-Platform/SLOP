#!/usr/bin/env python3
"""backend/scripts/update_recommendations.py

Smart post-update recommendation engine for ms-update.
Extracted from ms-update heredoc so it can be:
  - Scanned by Ruff (undefined names, dead code)
  - Scanned by Semgrep (architectural rule violations)
  - Mutation tested
  - Syntax-checked independently

Called by ms-update as:
  python3 backend/scripts/update_recommendations.py REPO PRE_COMMIT POST_COMMIT VENV FULL_MODE

Core Rule: all Python >50 lines must be in .py files, not shell heredocs.
"""

import sys
import json
import os
import pathlib
import re
import time
import subprocess
import select
import tty
import termios


def main() -> None:
    REPO = pathlib.Path(sys.argv[1])
    pre = sys.argv[2] if len(sys.argv) > 2 else ""
    post = sys.argv[3] if len(sys.argv) > 3 else ""
    VENV = sys.argv[4] if len(sys.argv) > 4 else ""
    FULL_MODE = sys.argv[5] == "1" if len(sys.argv) > 5 else False
    PY = str(pathlib.Path(VENV) / "bin" / "python3") if VENV else sys.executable

    CYAN = "\033[0;36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    DIM = "\033[2m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    NOW = time.time()
    DAY = 86400

    # ── What changed in this update ───────────────────────────────────────────
    changed_files: set[str] = set()
    if pre and post and pre != post:
        try:
            r = subprocess.run(
                ["git", "-C", str(REPO), "diff", "--name-only", pre, post],
                capture_output=True,
                text=True,
            )
            changed_files = set(r.stdout.splitlines())
        except Exception:  # noqa: S110  # best-effort changed-file detection; proceed without change list
            pass

    def changed(*patterns) -> bool:
        return any(re.search(p, f) for p in patterns for f in changed_files)

    api_changed = changed(r"backend/api/", r"backend/health/", r"backend/manifests/")
    wizard_changed = changed(r"wizard", r"SetupView", r"platform\.py")
    new_routes = changed(r"backend/api/", r"backend/core/")
    test_changed = changed(r"tests/test_", r"ms-test\.py")
    ms_update_changed = changed(r"ms-update", r"ms-check", r"backend/scripts/")

    # ── Test history ──────────────────────────────────────────────────────────
    hist_path = REPO / ".ms-test-history.json"
    runs: list = []
    try:
        data = json.loads(hist_path.read_text())
        runs = data.get("runs", [])
    except Exception:  # noqa: S110  # best-effort history load; proceed with empty run list
        pass

    # Full runs = have section B results
    def _is_full_run(r: dict) -> bool:
        """A full run has results for multiple sections (>3), not just section A."""
        s = r.get("summary", {})
        if len(s) > 3:
            return True
        # Also match any key starting with "B." regardless of suffix variation
        return any(
            k.startswith("B.") and (v.get("pass", 0) + v.get("fail", 0)) > 0 for k, v in s.items()
        )

    full_runs = [r for r in runs if _is_full_run(r)]

    last_full_ts: float = 0.0
    last_full_fails = 0
    last_full_passes = 0
    prev_pass_rate = 1.0

    if full_runs:
        last = full_runs[-1]
        try:
            from datetime import datetime

            ts = datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00"))
            last_full_ts = ts.timestamp()
        except Exception:  # noqa: S110  # best-effort timestamp parse; last_full_ts stays 0.0 if invalid
            pass
        last_full_fails = last.get("total_fail", 0)
        last_full_passes = last.get("total_pass", 0)
        if len(full_runs) >= 2:
            prev = full_runs[-2]
            pp = prev.get("total_pass", 0)
            pf = prev.get("total_fail", 0)
            prev_pass_rate = pp / (pp + pf) if (pp + pf) > 0 else 1.0

    # Static section failures from this update's ms-test run.
    # Use config.data_dir — same source ms-test.py writes from + production
    # uses (Core Rule 3.9). Hardcoded REPO/"data" diverges on non-default
    # MS_DATA_DIR deployments. Closes 1.1.5.k.
    static_failed = False
    try:
        sys.path.insert(0, str(REPO))
        from backend.core.config import config as _cfg

        _last_run_path = _cfg.data_dir / "last_test_run.json"
    except Exception:
        _last_run_path = REPO / "data" / "last_test_run.json"
    try:
        last_static = json.loads(_last_run_path.read_text())
        static_failed = last_static.get("failed", 0) > 0
    except Exception:  # noqa: S110  # best-effort last run load; static_failed stays False
        pass

    # Apps installed
    apps_installed = False
    try:
        import sqlite3

        db = REPO / "data" / "state.db"
        if db.exists():
            conn = sqlite3.connect(str(db))
            count = conn.execute(
                "SELECT COUNT(*) FROM apps WHERE status NOT IN ('disabled','failed')"
            ).fetchone()[0]
            apps_installed = count > 0
            conn.close()
    except Exception:  # noqa: S110  # best-effort DB query; apps_installed stays False
        pass

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    # Marker files track recommendation cadence
    def marker_age(name: str) -> float:
        p = REPO / f".ms-{name}-shown"
        try:
            return (NOW - p.stat().st_mtime) / DAY
        except Exception:  # marker file may not exist; treat as very old
            return 999.0

    def touch_marker(name: str) -> None:
        (REPO / f".ms-{name}-shown").touch()

    # ── Decision logic ────────────────────────────────────────────────────────
    days_since_full = (NOW - last_full_ts) / DAY if last_full_ts else 999

    cmds: list[tuple[str, list[str], str]] = []  # (id, argv, reason)

    # 1. Full live test
    full_reasons: list[str] = []
    if static_failed:
        full_reasons.append("static checks had failures this update")
    if api_changed and apps_installed:
        full_reasons.append("API/health code changed")
    if wizard_changed and apps_installed:
        full_reasons.append("wizard code changed")
    if days_since_full >= 7 and apps_installed:
        full_reasons.append(f"last full test was {int(days_since_full)}d ago")
    if last_full_fails > 0 and api_changed:
        full_reasons.append(f"{last_full_fails} failures in last full run")
    if ms_update_changed:
        full_reasons.append("ms-update or ms-check changed — verifying shell script integrity")

    if full_reasons:
        cmds.append(("test", [PY, str(REPO / "ms-test.py")], "; ".join(full_reasons[:2])))

    # 2. Trend (runs after full test so it shows fresh data)
    curr_rate = (
        last_full_passes / (last_full_passes + last_full_fails)
        if (last_full_passes + last_full_fails) > 0
        else 1.0
    )
    trend_reasons: list[str] = []
    if len(full_runs) >= 5 and curr_rate < prev_pass_rate - 0.05:
        trend_reasons.append(f"pass rate dropped {prev_pass_rate:.0%}→{curr_rate:.0%}")
    if len(runs) >= 10 and marker_age("trend") >= 7:
        trend_reasons.append(f"{len(runs)} runs recorded")

    if trend_reasons:
        cmds.append(("trend", [PY, str(REPO / "ms-test.py"), "--trend"], "; ".join(trend_reasons)))

    # 3. Self-improve (always last — uses fresh test data if full test just ran)
    si_reasons: list[str] = []
    if has_key and new_routes and marker_age("self-improve") >= 14:
        si_reasons.append("new API routes added")
    if has_key and test_changed and marker_age("self-improve") >= 14:
        si_reasons.append("test files changed")
    if has_key and marker_age("self-improve") >= 30 and apps_installed:
        si_reasons.append(f"scheduled ({int(marker_age('self-improve'))}d since last)")

    if si_reasons:
        cmds.append(
            ("self-improve", [PY, str(REPO / "ms-test.py"), "--self-improve"], si_reasons[0])
        )

    # Check if audit should run (significant structural changes)
    audit_worthy = changed(
        r"backend/api/",
        r"frontend/src/views/",
        r"catalog/apps/",
        r"backend/core/schema",
        r"backend/infra/providers/",
    )
    if audit_worthy and not FULL_MODE:
        snap_path = REPO / ".ms-audit-snapshot.json"
        if not snap_path.exists():
            cmds.append(
                ("audit", [PY, str(REPO / "ms-audit")], "first audit run — no previous snapshot")
            )
        else:
            cmds.append(
                ("audit", [PY, str(REPO / "ms-audit")], "API/frontend/manifest changes detected")
            )

    # --full mode: always run all three scripts regardless of heuristics
    if FULL_MODE:
        cmds = []
        cmds.append(("audit", [PY, str(REPO / "ms-audit")], "full mode — running full audit"))
        cmds.append(("test", [PY, str(REPO / "ms-test.py")], "full mode — running all tests"))
        if len(runs) >= 3:
            cmds.append(
                (
                    "trend",
                    [PY, str(REPO / "ms-test.py"), "--trend"],
                    "full mode — showing test history trend",
                )
            )
        if has_key:
            cmds.append(
                (
                    "self-improve",
                    [PY, str(REPO / "ms-test.py"), "--self-improve"],
                    "full mode — generating new tests",
                )
            )
        elif not has_key:
            print(f"  {DIM}(skipping --self-improve: ANTHROPIC_API_KEY not set){RESET}")

    if not cmds:
        sys.exit(0)

    # ── Countdown helper ──────────────────────────────────────────────────────
    def countdown_run(argv: list[str], label: str, reason: str) -> bool:
        """Print label with countdown. In full mode runs immediately (no countdown)."""
        SECS = 0 if FULL_MODE else 5
        msg = f"\n  {CYAN}{label}{RESET}  {DIM}({reason}){RESET}"
        print(msg)

        # Try to set raw mode so any keypress cancels (works in a real TTY)
        fd = sys.stdin.fileno() if sys.stdin.isatty() else -1
        old_settings = None
        if fd >= 0:
            try:
                old_settings = termios.tcgetattr(fd)
                tty.setraw(fd)
            except Exception:
                fd = -1

        cancelled = False
        try:
            for i in range(SECS, 0, -1):
                print(f"\r  Running in {i}s… (any key to skip) ", end="", flush=True)
                if fd >= 0:
                    r, _, _ = select.select([sys.stdin], [], [], 1.0)
                    if r:
                        sys.stdin.read(1)
                        cancelled = True
                        break
                else:
                    time.sleep(1.0)
        finally:
            if fd >= 0 and old_settings is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:  # noqa: S110  # best-effort terminal restore; unavailable if fd closed
                    pass

        if cancelled:
            print(f"\r  {YELLOW}↷ Skipped{RESET}{' ' * 30}")
            return False

        print(f"\r  {GREEN}▶ Running…{RESET}{' ' * 30}\n")
        try:
            subprocess.run(argv, cwd=str(REPO))
        except Exception as e:
            print(f"  Error: {e}")
        return True

    # ── Run in logical order ──────────────────────────────────────────────────
    print(f"\n  {BOLD}Recommended actions:{RESET}")
    for _, argv, _reason in cmds:
        label = " ".join(argv[1:])  # everything after the python binary
        if label.startswith(str(REPO)):
            label = "python3 ms-test.py" + label[len(str(REPO / "ms-test.py")) :]

    for cid, argv, reason in cmds:
        label = "python3 " + " ".join(
            a.replace(str(REPO) + "/", "").replace(str(REPO), ".") for a in argv[1:]
        )
        ran = countdown_run(argv, label, reason)
        if cid == "trend":
            touch_marker("trend")
        if cid == "self-improve" and ran:
            touch_marker("self-improve")

        # Save ms-test data to context assembler after full test runs
        if cid == "test" and ran:
            try:
                ctx_path = REPO / "data" / "last_ms_test_context.json"
                ctx_path.write_text(
                    json.dumps(
                        {
                            "ran_at": NOW,
                            "triggered_by": reason,
                            "code_changed": list(changed_files)[:20],
                            "days_since_previous": round(days_since_full, 1),
                        }
                    )
                )
            except Exception:  # noqa: S110  # best-effort context file write; non-critical metadata
                pass


if __name__ == "__main__":
    main()
