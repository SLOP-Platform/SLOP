"""F6b — seed starter-config files into an app's config dir before deploy.

Some catalog apps (e.g. glance) crash-loop on first start when their config
file is absent. A manifest may declare `seed_config:` — a list of
``{dest: "relative/path", content: "..."}`` entries — and these files are
written into the app's resolved ``config_path`` BEFORE ``docker compose up``.

Security: ``dest`` MUST be a relative path that stays inside ``config_path``.
Absolute paths and any ``..`` traversal are rejected. Existing files are left
untouched (idempotent — a reinstall never clobbers user edits).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from backend.manifests.executor import ExecutionResult
    from backend.manifests.loader import AppManifest


def seed_config_files(manifest: AppManifest, config_path: Path, result: ExecutionResult) -> bool:
    """Write manifest.seed_config files into config_path. Return False on error.

    On any unsafe/failed entry, calls result.fail(...) and returns False so the
    caller aborts the install before deploy.
    """
    entries: list[dict[str, Any]] = list(getattr(manifest, "seed_config", []) or [])
    if not entries:
        return True
    base = config_path.resolve()
    for entry in entries:
        dest = str(entry.get("dest", "") or "")
        content = str(entry.get("content", "") or "")
        if not dest:
            result.fail(
                "seed_config",
                "seed_config entry missing 'dest'.",
                "Each seed_config entry needs a relative 'dest' path.",
            )
            return False
        if os.path.isabs(dest):
            result.fail(
                "seed_config",
                f"Unsafe seed_config dest '{dest}'.",
                "dest must be relative, not absolute.",
            )
            return False
        try:
            resolved = (base / dest).resolve()
        except OSError as e:
            result.fail("seed_config", f"Bad seed_config dest '{dest}'.", str(e))
            return False
        if not (resolved == base or str(resolved).startswith(str(base) + os.sep)):
            result.fail(
                "seed_config",
                f"Unsafe seed_config dest '{dest}'.",
                "dest must stay inside the config dir (no '..').",
            )
            return False
        if resolved.exists():
            continue  # never clobber an existing config file
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content)
        except OSError as e:
            result.fail("seed_config", f"Could not write seed file '{dest}'.", str(e))
            return False
    result.add("seed_config", "ok", f"Seeded {len(entries)} config file(s).")
    return True
