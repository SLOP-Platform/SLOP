"""backend/agent/cost_probe.py — 24h rolling LLM spend probe.

GROUND check: compares rolling 24h cloud_llm_usage spend vs llm_budget from
settings. DRIFT if exceeded. Alert-only, never blocks.

Exit codes:
  0 — VERIFIED (spend within budget) or INDETERMINATE (free-only mode / DB missing)
  1 — DRIFT (24h spend exceeds llm_budget)

Usage::

    python3 backend/agent/cost_probe.py
"""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    # Gracefully skip when state.db doesn't exist (test environments, fresh installs).
    try:
        from backend.core.state import StateDB
    except Exception as e:
        print(f"INDETERMINATE: cannot import StateDB — {e}")
        return 0

    try:
        with StateDB() as db:
            # Read llm_budget from llm_agent_config blob.
            raw = db.get_setting("llm_agent_config")
            agent_cfg: dict[str, float] = json.loads(raw) if raw else {}
            llm_budget: float = float(agent_cfg.get("llm_budget") or 0.0)

            if llm_budget == 0.0:
                # Free-only mode — no cap to check.
                print("INDETERMINATE: llm_budget=0.0 (free-only mode, no cap to check)")
                return 0

            # Query rolling 24h spend from cloud_llm_usage.
            since = int(time.time()) - 86400
            row = db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS spend "
                "FROM cloud_llm_usage WHERE created_at >= ?",
                (since,),
            ).fetchone()
            spend_24h: float = float(row["spend"]) if row else 0.0

    except FileNotFoundError:
        print("INDETERMINATE: data/state.db not found — skipping")
        return 0
    except Exception as e:
        print(f"INDETERMINATE: DB error — {e}")
        return 0

    if spend_24h > llm_budget:
        print(f"DRIFT: 24h spend ${spend_24h:.4f} exceeds llm_budget ${llm_budget:.2f}")
        return 1

    print(f"VERIFIED: 24h spend ${spend_24h:.4f} within llm_budget ${llm_budget:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
