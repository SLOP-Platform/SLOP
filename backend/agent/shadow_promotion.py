"""Observe-only shadow->enforce promotion *reporter* (#1088, bounded un-gated slice).

The learned-confidence scorer (``backend/agent/classifier``) runs in SHADOW mode: it
computes a derived, outcome-weighted score and appends it to ``learning_shadow_log`` next
to the flat ``legacy_score`` (0.95) it would replace, but the legacy value still drives
behaviour. It cannot leave shadow mode without evidence that the derived scorer is *better*
than the flat cache — that gate is #1088.

This module ships only the **descriptive GROUND substrate** that gate consumes — it reads
the logged rows and reconciles ``learned_score`` against the per-row *actual outcome*
(success/(success+failure)). It deliberately makes **no promote/hold decision and bakes no
threshold**: choosing the promote threshold is a first-of-kind enforcement decision that is
review/DToC-gated (KL promotion rule). A reporter that silently picked a cutoff would BE that
decision. So the verdict here is only ``INDETERMINATE`` (not enough grounded evidence) vs
``OBSERVED`` (here are the stats) — never "ready to enforce".

Why the high/low band split: the legacy score is a *constant* 0.95, so it has zero
discriminative power by construction. The only thing that can justify the derived scorer is
that it *varies with outcome* — rows it scores higher succeed more often than rows it scores
lower. We surface that delta as raw GROUND; we do not judge whether it is "enough".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Minimum rows carrying a real outcome before any band comparison is meaningful. Below this
# the summary is INDETERMINATE (loud), never a quiet "no signal" that reads as "scorer bad".
MIN_OUTCOME_ROWS = 10

# ── Hysteresis window record (#1088 leg 5/6). ──────────────────────────────────────────────
# The promotion GATE (tools/audit_shadow_promotion.py) appends one record per INDEPENDENT
# advancing window (max_id strictly increased) that passes legs 1-4, to this repo-root file. The
# enforce flip (classifier._enforce_enabled, leg 6) reads the SAME file: it may only enable the
# derived scorer when >= MIN_PROMOTION_WINDOWS independent advancing PASS windows are recorded —
# so a static log cannot self-satisfy and the flag cannot flip on prose alone (binding, not theater).
PROMOTION_WINDOWS_FILE = ".shadow-promotion-windows.json"
MIN_PROMOTION_WINDOWS = 5


def load_promotion_windows(path: str | Path) -> list[dict[str, Any]]:
    """Read the recorded hysteresis windows (newest last). Missing/malformed → [] (fail-closed:
    no recorded evidence ⇒ not verified ⇒ enforce stays shadow)."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    windows = data.get("windows") if isinstance(data, dict) else None
    return windows if isinstance(windows, list) else []


def promotion_is_verified(windows: list[dict[str, Any]]) -> bool:
    """True iff the recorded windows satisfy the hysteresis floor: >= MIN_PROMOTION_WINDOWS
    PASS windows whose ``max_id`` values are STRICTLY INCREASING (independent re-derivations over
    an advancing log — a re-run of the same rows cannot count twice), AND the most recent window
    is itself a PASS (a stale historical pass followed by a regression does not promote)."""
    if not windows:
        return False
    if not windows[-1].get("passed"):
        return False
    last_id = -1
    independent_passes = 0
    for w in windows:
        if not w.get("passed"):
            continue
        mid = int(w.get("max_id", 0))
        if mid > last_id:
            independent_passes += 1
            last_id = mid
    return independent_passes >= MIN_PROMOTION_WINDOWS


@dataclass
class ShadowPromotionSummary:
    """Descriptive, observe-only reconciliation of the shadow log (no promote decision)."""

    verdict: str  # "OBSERVED" (stats below are grounded) | "INDETERMINATE" (insufficient)
    rows_total: int
    rows_with_outcome: int
    learned_min: float | None = None
    learned_max: float | None = None
    learned_varies: bool = False  # max>min — a degenerate (flat) scorer can never beat 0.95
    low_band_outcome_rate: float | None = None
    high_band_outcome_rate: float | None = None
    # high_band - low_band success-rate; POSITIVE means the scorer tracks outcome (the only
    # thing that can justify promotion). NOT compared to any threshold here (that is the gate).
    discrimination_delta: float | None = None
    notes: list[str] = field(default_factory=list)


def _outcome_rate(row: dict[str, Any]) -> float | None:
    """success / (success + failure) for one row, or None when the row has no outcome yet."""
    s = int(row.get("success_count", 0))
    f = int(row.get("failure_count", 0))
    total = s + f
    if total <= 0:
        return None
    return s / total


def summarize_shadow_log(rows: list[dict[str, Any]]) -> ShadowPromotionSummary:
    """Reconcile ``learned_score`` against actual outcome across *rows* — observe-only.

    *rows* are dicts as returned by ``StateDB.read_learning_shadow``. Returns a descriptive
    summary; emits ``INDETERMINATE`` (never a silent OK) when there is not enough grounded
    outcome evidence to compare bands. Makes no promote/hold call and bakes no threshold.
    """
    rows_total = len(rows)
    scored = [(float(r["learned_score"]), _outcome_rate(r)) for r in rows]
    with_outcome = [(ls, orate) for ls, orate in scored if orate is not None]
    rows_with_outcome = len(with_outcome)

    if rows_with_outcome < MIN_OUTCOME_ROWS:
        return ShadowPromotionSummary(
            verdict="INDETERMINATE",
            rows_total=rows_total,
            rows_with_outcome=rows_with_outcome,
            notes=[
                f"only {rows_with_outcome} row(s) carry an outcome "
                f"(need >= {MIN_OUTCOME_ROWS}) — insufficient grounded evidence to reconcile"
            ],
        )

    learned_vals = [ls for ls, _ in with_outcome]
    lo, hi = min(learned_vals), max(learned_vals)
    varies = hi > lo

    notes: list[str] = []
    if not varies:
        # A flat scorer is degenerate: it carries no more information than the constant 0.95.
        notes.append(
            "learned_score is constant across all outcome rows — degenerate (no discrimination possible)"
        )
        return ShadowPromotionSummary(
            verdict="OBSERVED",
            rows_total=rows_total,
            rows_with_outcome=rows_with_outcome,
            learned_min=lo,
            learned_max=hi,
            learned_varies=False,
            discrimination_delta=0.0,
            notes=notes,
        )

    # Split at the midpoint of the learned range; rows AT the midpoint go to the low band so
    # the high band is strictly "scored above the middle". Both bands are non-empty because
    # max>min guarantees at least one row on each side of the midpoint is possible — but a
    # skewed distribution can still empty one band, which we surface rather than divide-by-zero.
    mid = (lo + hi) / 2
    low_rates = [orate for ls, orate in with_outcome if ls <= mid]
    high_rates = [orate for ls, orate in with_outcome if ls > mid]

    low_rate = sum(low_rates) / len(low_rates) if low_rates else None
    high_rate = sum(high_rates) / len(high_rates) if high_rates else None
    if low_rate is None or high_rate is None:
        notes.append(
            "all outcome rows fell into a single learned-score band — cannot compare "
            "(distribution too skewed for a high/low split)"
        )
        delta = None
    else:
        delta = high_rate - low_rate
        notes.append(
            f"high-band (learned>{mid:.4f}) success-rate {high_rate:.3f} vs "
            f"low-band {low_rate:.3f}; delta {delta:+.3f} (positive => scorer tracks outcome). "
            "Promote-threshold is a separate review-gated decision — not applied here."
        )

    return ShadowPromotionSummary(
        verdict="OBSERVED",
        rows_total=rows_total,
        rows_with_outcome=rows_with_outcome,
        learned_min=lo,
        learned_max=hi,
        learned_varies=True,
        low_band_outcome_rate=low_rate,
        high_band_outcome_rate=high_rate,
        discrimination_delta=delta,
        notes=notes,
    )
