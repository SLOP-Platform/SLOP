"""Presentation helpers for `installer.uninstall` — pure output/prompt formatters.

Extracted from `installer/uninstall.py` (#1302 linecount drain) to keep the
uninstall module under its shrink-only baseline. These are behavior-preserving,
side-effect-free string/output builders (no module-level constants, no removals);
`installer.uninstall` re-exports `_print_clean_output` + `_clean_prompt` so existing
`from installer.uninstall import ...` references (incl. the test suite) keep resolving.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def _app_row_status(result: dict) -> tuple[str, str]:
    """Return (status, detail) for §C.6 output row."""
    ok = result.get("ok", False)
    steps = result.get("steps", [])
    error = result.get("error", "")

    if not ok:
        failed = [s for s in steps if s.get("status") == "error"]
        if failed:
            fs = failed[0]
            detail = f"{fs['name']} failed: {fs['message']}"
        else:
            detail = error or "unknown failure"
        return "failed", f"({detail})"

    warnings = [s for s in steps if s.get("status") == "warning"]
    if warnings:
        w = warnings[0]
        detail = f"removed; {w['name']} warning — see logs"
        return "warning", f"({detail})"

    return "ok", "(stopped, unwired, removed)"


def _print_clean_output(
    results: list[tuple[str, dict]],
    orphans: list[str],
    print_fn: Callable,
) -> int:
    """Print §C.6 per-app fidelity output. Returns exit code per §C.5."""
    print_fn("\nCleaning managed apps...\n")

    n_ok = n_warn = n_fail = 0
    for key, result in results:
        status, detail = _app_row_status(result)
        print_fn(f"  {key:<20} {status:<8} {detail}")
        if status == "ok":
            n_ok += 1
        elif status == "warning":
            n_warn += 1
        else:
            n_fail += 1

    if orphans:
        print_fn("\nOrphans (managed label without app-key):")
        for name in orphans:
            print_fn(f"  {name:<20} inspect with: docker inspect {name}")

    parts = []
    if n_ok:
        parts.append(f"{n_ok} ok")
    if n_warn:
        parts.append(f"{n_warn} warning")
    if n_fail:
        parts.append(f"{n_fail} failed")
    if orphans:
        parts.append(f"{len(orphans)} orphan")
    print_fn(f"\nSummary: {', '.join(parts)}")

    return 1 if n_fail > 0 else 0


def _clean_prompt(
    app_keys: list[str],
    orphans: list[str],
    hostname: str,
    port: int,
    data_dir: Path,
) -> str:
    lines = [f"About to reset all managed slop apps on {hostname}:\n"]
    for k in app_keys:
        lines.append(f"  - {k}")
    lines.append(
        "\nFor each app:\n"
        "  - Container will be stopped and removed\n"
        "  - Compose fragment will be removed\n"
        f"  - Per-app config under {data_dir}/config/<app>/ will be REMOVED\n"
        "    (delete_config=True; --keep-configs is not available in v5.0)\n\n"
        "SLOP itself will continue running. The wizard remains accessible\n"
        f"at http://{hostname}:{port}/ after 'clean' completes."
    )
    if orphans:
        lines.append(
            f"\n  Plus {len(orphans)} orphan container(s) (managed label without app-key)\n"
            "  that cannot be cleaned automatically — will be reported for manual inspection."
        )
    lines.append("\n\nContinue? [y/N]: ")
    return "\n".join(lines)
