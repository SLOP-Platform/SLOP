"""backend/platform/wizard_utils.py

Helper functions extracted from wizard.py to keep that module at or under
its 1030-line baseline.  Import target: backend.platform.wizard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.core.logging import get_logger
from backend.core.state import StateDB

if TYPE_CHECKING:
    from backend.platform.wizard import WizardInput, StepResult

log = get_logger(__name__)


def step_ntfy_config(inp: WizardInput) -> StepResult:
    """Persist ntfy notification settings to the state database.

    Runs after the Docker network step so infrastructure is confirmed
    reachable before we lock in the notification target.  Defaults:
      ntfy_enabled = True
      ntfy_url     = http://ntfy:80
      ntfy_topic   = slop
    """
    from backend.platform.wizard import StepResult

    try:
        with StateDB() as db:
            db.set_setting("ntfy_enabled", "true" if inp.ntfy_enabled else "false")
            db.set_setting("ntfy_url", inp.ntfy_url)
            db.set_setting("ntfy_topic", inp.ntfy_topic)
        return StepResult(
            step="ntfy_config",
            status="ok",
            message=(
                f"ntfy notifications {'enabled' if inp.ntfy_enabled else 'disabled'}. "
                f"URL: {inp.ntfy_url}, topic: {inp.ntfy_topic}."
            ),
        )
    except Exception as e:
        return StepResult(
            step="ntfy_config",
            status="error",
            message="Could not save ntfy settings.",
            detail=str(e),
        )
