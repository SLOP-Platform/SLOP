"""backend/core/llm_router.py

Intelligent LLM task router.

Selects the best enabled model for a given task type based on capability
scores and priority.  Falls back gracefully when no suitable model is found.

Task types
----------
reasoning     — multi-step diagnosis, root-cause analysis (Phi-4 excels)
json          — structured JSON output, parsing, extraction (Qwen excels)
code          — code review, log analysis (Qwen / CodeLlama)
fast          — single-word / classification, quick yes/no (SmolLM)
classification— labelling, categorization
general       — fallback for anything not matched above

Auto-detection heuristics (applied when a model is first registered)
----------------------------------------------------------------------
keyword in filename → capabilities added
  phi, phi4          → reasoning, general
  qwen               → json, code, reasoning
  smollm, smol       → fast, classification, general
  llama, mistral     → general, reasoning
  gemma              → general
  codellama          → code
  deepseek           → code, reasoning
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from backend.core.logging import get_logger
from backend.core.sqlbuild import build_update

log = get_logger(__name__)

# ── Capability auto-detection ─────────────────────────────────────────────

_CAPABILITY_RULES: list[tuple[str, list[str]]] = [
    (r"phi[-_]?4", ["reasoning", "general"]),
    (r"phi[-_]?3", ["reasoning", "fast"]),
    (r"phi", ["reasoning", "general"]),
    (r"qwen", ["json", "code", "reasoning"]),
    (r"smollm|smol[-_]lm", ["fast", "classification", "general"]),
    (r"llama", ["general", "reasoning"]),
    (r"mistral", ["general", "reasoning"]),
    (r"mixtral", ["general", "reasoning"]),
    (r"gemma", ["general"]),
    (r"codellama", ["code"]),
    (r"deepseek", ["code", "reasoning"]),
    (r"starcoder", ["code"]),
]

# Default task scores per capability
_CAPABILITY_SCORES: dict[str, dict[str, float]] = {
    "reasoning": {"reasoning": 0.95, "general": 0.7},
    "json": {"json": 0.95, "code": 0.8},
    "code": {"code": 0.95, "json": 0.7},
    "fast": {"fast": 0.95, "classification": 0.9, "general": 0.5},
    "classification": {"classification": 0.9, "fast": 0.8},
    "general": {"general": 0.7, "reasoning": 0.6, "json": 0.5},
}


def detect_capabilities(filename: str) -> list[str]:
    """Infer capabilities from a GGUF filename."""
    name = filename.lower()
    caps: set[str] = set()
    for pattern, abilities in _CAPABILITY_RULES:
        if re.search(pattern, name):
            caps.update(abilities)
    if not caps:
        caps.add("general")
    return sorted(caps)


def default_task_scores(capabilities: list[str]) -> dict[str, float]:
    """Build default task scores from a capability list."""
    scores: dict[str, float] = {}
    for cap in capabilities:
        for task, score in _CAPABILITY_SCORES.get(cap, {}).items():
            scores[task] = max(scores.get(task, 0.0), score)
    # Ensure general fallback
    scores.setdefault("general", 0.4)
    return scores


# ── Model record ──────────────────────────────────────────────────────────


@dataclass
class ModelRecord:
    filename: str
    display_name: str
    enabled: bool
    capabilities: list[str]
    task_scores: dict[str, float]
    priority: int
    context_window: int
    ollama_name: str | None
    notes: str

    @classmethod
    def from_row(cls, row: Any) -> ModelRecord:
        return cls(
            filename=row["filename"],
            display_name=row["display_name"] or row["filename"].replace(".gguf", ""),
            enabled=bool(row["enabled"]),
            capabilities=json.loads(row["capabilities"] or "[]"),
            task_scores=json.loads(row["task_scores"] or "{}"),
            priority=row["priority"],
            context_window=row["context_window"],
            ollama_name=row["ollama_name"],
            notes=row["notes"] or "",
        )

    def score_for(self, task_type: str) -> float:
        """Return this model's score for a task type (0-1)."""
        explicit = self.task_scores.get(task_type)
        if explicit is not None:
            return explicit
        # Fall back to general if task not explicitly scored
        if task_type not in ("general",):
            return self.task_scores.get("general", 0.0) * 0.8
        return 0.0


# ── Router ────────────────────────────────────────────────────────────────


def get_all_models() -> list[ModelRecord]:
    """Return all registry entries."""
    from backend.core.state import StateDB

    with StateDB() as db:
        rows = db.execute(
            "SELECT * FROM llm_model_registry ORDER BY priority ASC, filename ASC"
        ).fetchall()
    return [ModelRecord.from_row(r) for r in rows]


def get_enabled_models() -> list[ModelRecord]:
    return [m for m in get_all_models() if m.enabled]


def best_model_for(task_type: str) -> ModelRecord | None:
    """Return the highest-scoring enabled model for a task type."""
    candidates = get_enabled_models()
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda m: (-m.score_for(task_type), m.priority),
    )
    winner = ranked[0]
    log.debug(
        "LLM router: task=%s → %s (score=%.2f)",
        task_type,
        winner.filename,
        winner.score_for(task_type),
    )
    return winner


def routing_table() -> list[dict[str, Any]]:
    """Return the full routing table for UI display."""
    task_types = ["reasoning", "json", "code", "fast", "classification", "general"]
    table = []
    for task in task_types:
        winner = best_model_for(task)
        table.append(
            {
                "task_type": task,
                "model": winner.filename if winner else None,
                "display_name": winner.display_name if winner else None,
                "score": round(winner.score_for(task), 2) if winner else 0,
            }
        )
    return table


# ── Registry management ───────────────────────────────────────────────────


def sync_registry_with_files(filenames: list[str]) -> None:
    """Add new GGUF files to registry (auto-detect capabilities).
    Existing entries are untouched. Missing files are left in registry
    so their settings persist if the file reappears.
    """
    from backend.core.state import StateDB

    with StateDB() as db:
        existing = {
            r["filename"] for r in db.execute("SELECT filename FROM llm_model_registry").fetchall()
        }
        for fname in filenames:
            if fname in existing:
                continue
            caps = detect_capabilities(fname)
            scores = default_task_scores(caps)
            db.execute(
                """INSERT INTO llm_model_registry
                   (filename, capabilities, task_scores, enabled)
                   VALUES (?, ?, ?, 0)""",
                (fname, json.dumps(caps), json.dumps(scores)),
            )
            log.info("LLM registry: auto-registered %s caps=%s", fname, caps)


def upsert_model(
    filename: str,
    *,
    enabled: bool | None = None,
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    task_scores: dict[str, float] | None = None,
    priority: int | None = None,
    context_window: int | None = None,
    ollama_name: str | None = None,
    notes: str | None = None,
) -> None:
    """Update registry entry for a model."""
    from backend.core.state import StateDB
    import time as _t

    with StateDB() as db:
        existing = db.execute(
            "SELECT * FROM llm_model_registry WHERE filename = ?", (filename,)
        ).fetchone()
        if not existing:
            caps = capabilities or detect_capabilities(filename)
            scores = task_scores or default_task_scores(caps)
            db.execute(
                """INSERT INTO llm_model_registry
                   (filename, display_name, enabled, capabilities, task_scores,
                    priority, context_window, ollama_name, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    filename,
                    display_name,
                    int(enabled or 0),
                    json.dumps(caps),
                    json.dumps(scores),
                    priority or 5,
                    context_window or 4096,
                    ollama_name,
                    notes,
                ),
            )
        else:
            updates: dict[str, Any] = {"updated_at": int(_t.time())}
            if enabled is not None:
                updates["enabled"] = int(enabled)
            if display_name is not None:
                updates["display_name"] = display_name
            if capabilities is not None:
                updates["capabilities"] = json.dumps(capabilities)
            if task_scores is not None:
                updates["task_scores"] = json.dumps(task_scores)
            if priority is not None:
                updates["priority"] = priority
            if context_window is not None:
                updates["context_window"] = context_window
            if ollama_name is not None:
                updates["ollama_name"] = ollama_name
            if notes is not None:
                updates["notes"] = notes
            db.execute(*build_update("llm_model_registry", updates, "filename = ?", (filename,)))


def remove_model_from_registry(filename: str) -> None:
    from backend.core.state import StateDB

    with StateDB() as db:
        db.execute("DELETE FROM llm_model_registry WHERE filename = ?", (filename,))
