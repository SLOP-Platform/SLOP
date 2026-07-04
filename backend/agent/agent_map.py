"""backend/agent/agent_map.py — static read-only agent-map render (#986 core).

A deterministic, structured projection of the Action Registry
(``backend.agent.registry``) suitable for a STATIC render (a generated artifact,
an n8n consumer, a diagram) — distinct from the existing FLAT projections
(``registry.list_actions`` / ``GET /api/v1/health/agent-actions`` return an
ungrouped list; chat's ``_list_actions_reply`` renders a bullet list). This
module's value-add is the GROUPED map (by tier, by diagnosis-class, with counts)
that a renderer/consumer needs and which nothing else produces — it does NOT
re-expose the flat list (Reuse-and-blast-radius checkpoint: build ON
``list_actions``, never duplicate it).

READ-ONLY by construction: it derives entirely from ``list_actions()``, which
returns :class:`registry.ActionView` (a frozen projection that NEVER carries a
handler callable). So the rendered map is pure data — JSON-serializable, with no
executor reference to leak.

The optional n8n consumer and the catalog license gate named in #986 are
explicit FOLLOW-ONS (deferred) — this is the static-render core they would build
on. Invoke as a CLI to emit the static artifact:

    python3 -m backend.agent.agent_map        # prints the JSON map to stdout
"""

from __future__ import annotations

import json
from collections import defaultdict

from backend.agent.registry import ActionView, list_actions


def _action_dict(view: ActionView) -> dict[str, object]:
    """One action as plain JSON-serializable data (diagnosis_classes sorted for
    determinism). Mirrors ActionView's fields — never the handler (it has none)."""
    return {
        "id": view.id,
        "tier": view.tier,
        "reversible": view.reversible,
        "executable": view.executable,
        "scopeable": view.scopeable,
        "default_rate_limit": view.default_rate_limit,
        "diagnosis_classes": sorted(view.diagnosis_classes),
        "description": view.description,
    }


def render_agent_map(views: list[ActionView] | None = None) -> dict[str, object]:
    """The grouped, deterministic agent map derived from the registry.

    *views* defaults to ``list_actions()`` (the live registry); it is injectable
    for hermetic testing. The result is stable across calls (every collection is
    sorted) and fully JSON-serializable — no callable, no spec object.

    Shape::

        {
          "actions": [ <action dict>, ... ],          # sorted by id
          "by_tier": { "<tier>": [ <id>, ... ] },     # tier -> sorted ids
          "by_diagnosis_class": { "<cls>": [ <id> ] },# class -> sorted ids
          "counts": { "total", "executable", "pending", "reversible",
                      "scopeable", "by_tier": { "<tier>": <n> } },
        }
    """
    if views is None:
        views = list_actions()

    actions = sorted((_action_dict(v) for v in views), key=lambda a: str(a["id"]))

    by_tier: dict[str, list[str]] = defaultdict(list)
    by_diagnosis_class: dict[str, list[str]] = defaultdict(list)
    tier_counts: dict[str, int] = defaultdict(int)
    for view in views:
        tier_key = str(view.tier)
        by_tier[tier_key].append(view.id)
        tier_counts[tier_key] += 1
        # dict.fromkeys dedups a malformed spec whose diagnosis_classes repeat a
        # class — without it the same id would land in a bucket twice, violating the
        # set-like bucket contract (a JSON array of dupes round-trips silently).
        for cls in dict.fromkeys(view.diagnosis_classes):
            by_diagnosis_class[cls].append(view.id)

    executable = sum(1 for v in views if v.executable)
    return {
        "actions": actions,
        "by_tier": {tier: sorted(ids) for tier, ids in sorted(by_tier.items())},
        "by_diagnosis_class": {cls: sorted(ids) for cls, ids in sorted(by_diagnosis_class.items())},
        "counts": {
            "total": len(views),
            "executable": executable,
            "pending": len(views) - executable,
            "reversible": sum(1 for v in views if v.reversible),
            "scopeable": sum(1 for v in views if v.scopeable),
            "by_tier": {tier: tier_counts[tier] for tier in sorted(tier_counts)},
        },
    }


def render_agent_map_json(*, indent: int = 2) -> str:
    """The agent map as a deterministic JSON string (the static artifact body).

    ``sort_keys=True`` + the sorted collections above make the output byte-stable
    for the same registry — so a generated artifact only changes when the registry
    does (a clean diff for a committed static map / an n8n consumer)."""
    return json.dumps(render_agent_map(), indent=indent, sort_keys=True)


if __name__ == "__main__":
    print(render_agent_map_json())
