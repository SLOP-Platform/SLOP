"""backend/api/quickstart.py — QuickStart wizard state API."""

from __future__ import annotations
import time
from typing import Any
from collections.abc import Callable
from fastapi import APIRouter
from pydantic import BaseModel
from backend.core.config import config as _config

# Step 4 followup: prefix moved from `/api/quickstart` to bare
# `/quickstart` so the router mounts at both /api/v1/quickstart and
# /api/quickstart via the standard `_mount()` helper (matching every
# other router). Previously the baked-in `/api/quickstart` prefix
# caused the v1 mount to land at `/api/v1/api/quickstart`.
router = APIRouter(prefix="/quickstart", tags=["QuickStart"])

PHASES: list[dict[str, Any]] = [
    {
        "id": "platform",
        "label": "Platform check",
        "route": None,
        "optional": False,
        "description": "Verify dependencies and configuration are healthy.",
    },
    {
        "id": "traefik",
        "label": "Deploy Traefik",
        "route": "/infra",
        "optional": False,
        "description": "Reverse proxy that routes traffic to all your apps.",
    },
    {
        "id": "auth",
        "label": "Set up auth",
        "route": "/infra",
        "optional": True,
        "description": "Protect your apps with TinyAuth or Authelia.",
    },
    {
        "id": "tunnel",
        "label": "Configure tunnel",
        "route": "/infra",
        "optional": True,
        "description": "Expose apps externally via Cloudflare or Tailscale.",
    },
    {
        "id": "storage",
        "label": "Configure storage",
        "route": "/storage",
        "optional": False,
        "description": "Set local media paths and optional cloud sources.",
    },
    {
        "id": "routing",
        "label": "Configure routing",
        "route": "/routing",
        "optional": False,
        "description": "Choose which apps handle movies, TV, music, etc.",
    },
    {
        "id": "apps",
        "label": "Install apps",
        "route": "/catalog",
        "optional": False,
        "description": "Install your first media stack apps via Quick Stacks.",
    },
    {
        "id": "llm",
        "label": "Set up AI agent",
        "route": "/models",
        "optional": True,
        "description": "Download a model for AI-powered health diagnostics.",
    },
]


def _ensure_table(db: Any) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS quickstart_phases (
            phase TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            completed_at INTEGER
        )
    """)


def _get_phases(db: Any) -> list[dict[str, Any]]:
    # dict(r): db.execute() yields raw sqlite3.Row objects, which have no .get();
    # convert so the row.get(...) lookups below work when a phase row exists.
    rows = {
        r["phase"]: dict(r)
        for r in db.execute("SELECT phase, status, completed_at FROM quickstart_phases").fetchall()
    }
    result = []
    for p in PHASES:
        row = rows.get(p["id"], {})
        result.append(
            {**p, "status": row.get("status", "pending"), "completed_at": row.get("completed_at")}
        )
    return result


@router.get("")
def get_quickstart() -> dict[str, Any]:
    """Return QuickStart wizard state including whether it should be shown."""
    from backend.core.state import StateDB

    with StateDB() as db:
        _ensure_table(db)
        phases = _get_phases(db)
        # Auto-detect completion for phases that have clear DB signals
        _now = int(time.time())
        _auto_phase_checks: dict[str, Callable[[], bool]] = {
            "platform": lambda: (
                db.execute("SELECT COUNT(*) FROM infra_slots WHERE status='active'").fetchone()[0]
                > 0
                or db.execute("SELECT COUNT(*) FROM apps").fetchone()[0] > 0
            ),
            "traefik": lambda: (
                __import__("pathlib")
                .Path(db.get_setting("compose_dir") or str(_config.compose_dir))
                .joinpath("traefik.yaml")
                .exists()
            ),
            "auth": lambda: (
                db.execute(
                    "SELECT COUNT(*) FROM infra_slots WHERE slot='auth' AND status='active'"
                ).fetchone()[0]
                > 0
            ),
            "tunnel": lambda: (
                db.execute(
                    "SELECT COUNT(*) FROM infra_tunnel_providers WHERE status='active'"
                ).fetchone()[0]
                > 0
            ),
            "storage": lambda: (
                db.execute("SELECT COUNT(*) FROM storage_sources WHERE status='active'").fetchone()[
                    0
                ]
                > 0
            ),
            "apps": lambda: (
                db.execute(
                    "SELECT COUNT(*) FROM apps WHERE status NOT IN ('removing','disabled')"
                ).fetchone()[0]
                > 0
            ),
            "llm": lambda: (
                db.execute("SELECT COUNT(*) FROM llm_model_registry WHERE enabled=1").fetchone()[0]
                > 0
            ),
        }
        _changed = False
        for _phase_id, _check_fn in _auto_phase_checks.items():
            _phase = next((p for p in phases if p["id"] == _phase_id), None)
            if _phase and _phase["status"] == "pending":
                try:
                    if _check_fn():
                        db.execute(
                            "INSERT OR REPLACE INTO quickstart_phases (phase, status, completed_at) VALUES (?, 'complete', ?)",
                            (_phase_id, _now),
                        )
                        _changed = True
                except Exception:  # noqa: S110  # best-effort phase auto-check; skip if DB query fails
                    pass
        if _changed:
            phases = _get_phases(db)

        completed = sum(1 for p in phases if p["status"] in ("complete", "skipped"))
        total_required = sum(1 for p in phases if not p["optional"])
        required_done = sum(
            1 for p in phases if not p["optional"] and p["status"] in ("complete", "skipped")
        )

        # Show wizard if < 50% of required phases are done and not explicitly dismissed
        dismissed = db.get_setting("quickstart_dismissed") == "1"
        show = not dismissed and required_done < total_required

        return {
            "show": show,
            "dismissed": dismissed,
            "phases": phases,
            "completed": completed,
            "total": len(phases),
            "required_done": required_done,
            "total_required": total_required,
            "percent": int(required_done / total_required * 100) if total_required else 100,
        }


class PhaseUpdate(BaseModel):
    status: str  # complete | skipped | pending


@router.put("/{phase_id}")
def update_phase(phase_id: str, req: PhaseUpdate) -> dict[str, Any]:
    """Mark a phase complete, skipped, or reset to pending."""
    from backend.core.state import StateDB

    valid_ids = {p["id"] for p in PHASES}
    if phase_id not in valid_ids:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Unknown phase: {phase_id}")
    with StateDB() as db:
        _ensure_table(db)
        db.execute(
            """INSERT OR REPLACE INTO quickstart_phases (phase, status, completed_at)
               VALUES (?, ?, ?)""",
            (phase_id, req.status, int(time.time()) if req.status == "complete" else None),
        )
    return {"ok": True}


@router.post("/dismiss")
def dismiss_quickstart() -> dict[str, Any]:
    """Permanently dismiss the QuickStart wizard."""
    from backend.core.state import StateDB

    with StateDB() as db:
        db.set_setting("quickstart_dismissed", "1")
    return {"ok": True}


@router.post("/reset")
def reset_quickstart() -> dict[str, Any]:
    """Reset QuickStart wizard — re-shows it from the beginning."""
    from backend.core.state import StateDB

    with StateDB() as db:
        _ensure_table(db)
        db.execute("DELETE FROM quickstart_phases")
        db.set_setting("quickstart_dismissed", "0")
    return {"ok": True}
