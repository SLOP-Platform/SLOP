"""backend/health/anomaly.py

Anomaly detection — correlates patterns across health check cycles.

Finds recurring issues like:
  - "Sonarr always errors at 3am on Tuesdays"
  - "Plex HTTP check fails whenever Radarr is importing"
  - "Database locked occurs every 7 days (backup schedule conflict)"

This is time-series analysis, not LLM learning. The LLM interprets the
patterns; it doesn't produce them. Results are surfaced in the health view
as "Recurring issues".
"""

from __future__ import annotations

from typing import Any

import time
from collections import defaultdict
from dataclasses import dataclass

from backend.core.logging import get_logger
from datetime import UTC

log = get_logger(__name__)


@dataclass
class AnomalyPattern:
    app_key: str
    check_name: str
    occurrences: int
    first_seen: int  # unix timestamp
    last_seen: int
    typical_hour: int | None  # hour of day where failures cluster (0-23)
    typical_day: int | None  # day of week (0=Mon, 6=Sun)
    description: str
    is_recurring: bool = False


def detect_anomalies(lookback_hours: int = 168) -> list[AnomalyPattern]:
    """Analyse health check history for recurring failure patterns.

    lookback_hours: how far back to look (default 7 days).
    Returns patterns with 3+ occurrences (potential recurring issues).

    Step 2.7.d: orchestrator delegates phases to helpers
    (`_load_failure_events`, `_group_failure_events`,
    `_build_maintenance_index`, `_pattern_for_group`) — drops
    complexity from 16 to ≤ 4.
    """
    cutoff = int(time.time()) - lookback_hours * 3600

    rows, mw_rows = _load_failure_events(cutoff)
    if not rows:
        return []

    groups = _group_failure_events(rows)
    mw_index = _build_maintenance_index(mw_rows)

    patterns: list[AnomalyPattern] = []
    for (app_key, check_name), events in groups.items():
        pat = _pattern_for_group(
            app_key,
            check_name,
            events,
            mw_index,
            lookback_hours,
        )
        if pat is not None:
            patterns.append(pat)

    patterns.sort(key=lambda p: p.occurrences, reverse=True)
    return patterns


# ── _detect_anomalies helpers (step 2.7.d) ──────────────────────────


def _load_failure_events(cutoff: int) -> tuple[list[Any], list[Any]]:
    """Load failure events + maintenance windows from the DB.

    Returns `(rows, mw_rows)`. Returns `([], [])` on any DB error or
    when the history table is absent — anomaly detection becomes a
    no-op rather than a hard failure for these cases.
    """
    from backend.core.state import StateDB

    try:
        with StateDB() as db:
            history_exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='health_check_history'"
            ).fetchall()
            if not history_exists:
                return [], []
            rows = db.execute(
                """
                SELECT h.subject_key as app_key, h.check_name, h.status,
                       h.summary, h.checked_at
                FROM health_check_history h
                INNER JOIN apps a ON a.key = h.subject_key
                WHERE h.status IN ('error', 'warning')
                AND h.subject_type = 'app'
                AND h.checked_at >= ?
                ORDER BY h.checked_at ASC
                """,
                (cutoff,),
            ).fetchall()
            try:
                mw_rows = db.execute(
                    "SELECT * FROM maintenance_windows WHERE enabled = 1"
                ).fetchall()
            except Exception:
                mw_rows = []
            return rows, mw_rows
    except Exception as e:
        log.debug("Anomaly detection skipped: %s", e)
        return [], []


def _group_failure_events(
    rows: list[Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Bucket failure events by (app_key, check_name)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["app_key"], row["check_name"])].append(
            {
                "timestamp": row["checked_at"],
                "summary": row["summary"] if row["summary"] else "",
            }
        )
    return groups


def _build_maintenance_index(
    mw_rows: list[Any],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Build a (app_key, check_name) → window list index from
    maintenance window DB rows. `hour_end == -1` means a 2h window
    starting at `hour_start`."""
    mw_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for mw in mw_rows:
        end_h = mw["hour_end"] if mw["hour_end"] != -1 else mw["hour_start"] + 2
        mw_index[(mw["app_key"], mw["check_name"])].append(
            {
                "day": mw["day_of_week"],  # None = every day
                "h_start": mw["hour_start"],
                "h_end": end_h,
            }
        )
    return mw_index


def _is_in_maintenance(
    ts: int,
    app: str,
    check: str,
    mw_index: dict[tuple[str, str], list[dict[str, Any]]],
) -> bool:
    """True iff timestamp `ts` lies inside a maintenance window for (app, check)."""
    from datetime import datetime

    dt = datetime.fromtimestamp(ts, tz=UTC)
    for w in mw_index.get((app, check), []):
        if w["day"] is not None and dt.weekday() != w["day"]:
            continue
        if w["h_start"] <= dt.hour < w["h_end"]:
            return True
    return False


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _describe_typical_time(typical_hour: int | None, typical_day: int | None) -> str:
    """Render the human description suffix for failure clustering."""
    if typical_hour is not None and typical_day is not None:
        return f" — often around {typical_hour:02d}:00 on {_DAY_NAMES[typical_day]}"
    if typical_hour is not None:
        return f" — often around {typical_hour:02d}:00"
    if typical_day is not None:
        return f" — often on {_DAY_NAMES[typical_day]}"
    return ""


def _pattern_for_group(
    app_key: str,
    check_name: str,
    events: list[dict[str, Any]],
    mw_index: dict[tuple[str, str], list[dict[str, Any]]],
    lookback_hours: int,
) -> AnomalyPattern | None:
    """Build an AnomalyPattern for one (app, check) group, or None
    when the group has fewer than 3 non-maintenance events."""
    events = [
        e for e in events if not _is_in_maintenance(e["timestamp"], app_key, check_name, mw_index)
    ]
    n = len(events)
    if n < 3:
        return None

    from datetime import datetime

    timestamps = [e["timestamp"] for e in events]
    hours = [datetime.fromtimestamp(t, tz=UTC).hour for t in timestamps]
    days = [datetime.fromtimestamp(t, tz=UTC).weekday() for t in timestamps]
    typical_hour = _mode_if_dominant(hours, threshold=0.4)
    typical_day = _mode_if_dominant(days, threshold=0.4)
    time_desc = _describe_typical_time(typical_hour, typical_day)

    description = (
        f"{check_name} has failed {n} times in the last {lookback_hours // 24} days{time_desc}."
    )
    has_window = bool(mw_index.get((app_key, check_name)))
    is_scheduled = (typical_hour is not None or typical_day is not None) and has_window
    return AnomalyPattern(
        app_key=app_key,
        check_name=check_name,
        occurrences=n,
        first_seen=min(timestamps),
        last_seen=max(timestamps),
        typical_hour=typical_hour,
        typical_day=typical_day,
        description=description,
        is_recurring=n >= 5 and not is_scheduled,
    )


def _mode_if_dominant(values: list[int], threshold: float = 0.4) -> int | None:
    """Return the most common value if it appears in at least `threshold` fraction."""
    if not values:
        return None
    counts: dict[int, int] = defaultdict(int)
    for v in values:
        counts[v] += 1
    best = max(counts, key=lambda k: counts[k])
    if counts[best] / len(values) >= threshold:
        return best
    return None


def get_anomaly_summary() -> list[dict[str, Any]]:
    """Return anomaly patterns as dicts for the API/UI."""
    try:
        patterns = detect_anomalies()
        return [
            {
                "app_key": p.app_key,
                "check_name": p.check_name,
                "occurrences": p.occurrences,
                "last_seen": p.last_seen,
                "is_recurring": p.is_recurring,
                "description": p.description,
                "typical_hour": p.typical_hour,
                "typical_day": p.typical_day,
                "can_schedule": p.typical_hour is not None or p.typical_day is not None,
            }
            for p in patterns[:10]  # top 10
        ]
    except Exception as e:
        log.debug("Anomaly summary failed: %s", e)
        return []
