"""backend/agent/integrity.py

Process-integrity check for the SLOP Agent (executive manager).

The SLOP Agent's job is not only to watch catalog apps — it is also accountable
for SLOP's own rule-enforcement coverage.  This module shells out to the
``ms-coverage`` script, reads ``data/coverage_map.json``, and reports how many
rule nodes are still uncovered, grouped by risk.

``run_process_integrity_check()`` is the only public entry point.  It MUST NOT
raise: every failure path returns an ``IntegrityResult(ok=False, ...)`` with a
human-readable summary so the health cycle can write a row regardless.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from pathlib import Path

from backend.core.logging import get_logger

log = get_logger(__name__)

REPO: Path = Path(__file__).resolve().parents[2]
_COVERAGE_SCRIPT: Path = REPO / "ms-coverage"
_COVERAGE_MAP: Path = REPO / "data" / "coverage_map.json"
_TIMEOUT_S: float = 120.0


@dataclass
class IntegrityResult:
    ok: bool
    critical_gaps: int = 0
    high_gaps: int = 0
    total_rules: int = 0
    summary: str = ""


def _count_gaps(nodes: list[dict[str, Any]]) -> tuple[int, int, int]:
    critical = 0
    high = 0
    total = 0
    for n in nodes:
        if n.get("kind") != "rule":
            continue
        total += 1
        if n.get("covered") is False:
            risk = (n.get("risk") or "").lower()
            if risk == "critical":
                critical += 1
            elif risk == "high":
                high += 1
    return critical, high, total


def run_process_integrity_check() -> IntegrityResult:
    """Run ms-coverage, parse the coverage map, count uncovered rule nodes.

    Returns an IntegrityResult — never raises.  ``ok`` is True only when no
    critical gaps remain.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(_COVERAGE_SCRIPT), "--json"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            return IntegrityResult(
                ok=False,
                summary=(
                    "integrity check failed: ms-coverage exit "
                    f"{proc.returncode}: {(proc.stderr or '').strip()[:200]}"
                ),
            )

        if not _COVERAGE_MAP.exists():
            return IntegrityResult(
                ok=False,
                summary=f"integrity check failed: coverage map missing at {_COVERAGE_MAP}",
            )

        data = json.loads(_COVERAGE_MAP.read_text())
        nodes = data.get("nodes") or []
        critical, high, total = _count_gaps(nodes)
        ok = critical == 0

        if total == 0:
            summary = "integrity check produced no rule nodes — coverage map empty"
            return IntegrityResult(ok=False, total_rules=0, summary=summary)

        if ok and high == 0:
            summary = f"all {total} rules covered (no critical or high gaps)"
        elif ok:
            summary = f"{total} rules tracked; {high} high-risk gap(s), no critical gaps"
        else:
            summary = f"{total} rules tracked; {critical} critical and {high} high-risk gap(s)"

        return IntegrityResult(
            ok=ok,
            critical_gaps=critical,
            high_gaps=high,
            total_rules=total,
            summary=summary,
        )
    except subprocess.TimeoutExpired:
        return IntegrityResult(
            ok=False,
            summary=f"integrity check failed: ms-coverage timed out after {_TIMEOUT_S:.0f}s",
        )
    except json.JSONDecodeError as exc:
        return IntegrityResult(
            ok=False,
            summary=f"integrity check failed: coverage map is not valid JSON: {exc}",
        )
    except Exception as exc:
        log.warning("run_process_integrity_check unexpected error: %s", exc)
        return IntegrityResult(
            ok=False,
            summary=f"integrity check failed: {exc}",
        )
