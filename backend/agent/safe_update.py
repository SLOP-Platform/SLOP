"""backend/agent/safe_update.py — per-app SAFE image update with rollback.

The naive self-heal ``pull_image`` action (``backend/health/checker._heal_pull_image``)
does a blind ``docker pull`` and stops there: it never restarts the container,
never verifies the result, and cannot undo a bad image.  That is the same shape
the platform's ``ms-update`` script deliberately avoids for the deployment as a
whole — it pulls, restarts, **tests**, and **rolls back on failure**.

This module brings that SAFE-update discipline to a single application container:

    capture current image id → pull tag → restart → verify healthy
        └─ on verify failure → re-point tag to the captured id → restart (rollback)

It reuses :func:`backend.agent.verify.verify_container_healthy` for the test leg
(same poller the safe-fix tier already trusts) rather than re-implementing a
health wait.  It is autonomy-gated by the caller (see ``apply.py`` /
``health/checker.py``): an agent-initiated SAFE-update only EXECUTES when the
operational level is AUTONOMOUS and the per-app auto-update preference is on;
otherwise it is proposed, never run silently.

Never raises for an expected docker failure — returns a structured result the
caller records.  A disruptive container update is exactly the class of action
that must never fire without an explicit autonomy decision upstream.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from typing import Any
from collections.abc import Iterator

from backend.agent.types import OperationalLevel
from backend.agent.verify import verify_container_healthy
from backend.core.logging import get_logger

log = get_logger(__name__)

SafeUpdateResult = dict[str, Any]  # {"ok": bool, "message": str, "rolled_back": bool}
GateDecision = dict[str, Any]  # {"execute": bool, "reason": str}

_PULL_TIMEOUT_S = 120
_RESTART_TIMEOUT_S = 30
_INSPECT_TIMEOUT_S = 10


def _current_image_id(container: str) -> str | None:
    """Return the running container's resolved image id (sha256:...), or None.

    This is the daemon's content-addressed handle for the image the container is
    currently on; it remains valid as a rollback target even after a ``pull``
    moves the tag to a newer digest.
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("safe_update: inspect failed for %s: %s", container, exc)
        return None
    if r.returncode != 0:
        return None
    image_id = r.stdout.strip()
    return image_id or None


def _pull(image_ref: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "pull", image_ref],
            capture_output=True,
            timeout=_PULL_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("safe_update: pull failed for %s: %s", image_ref, exc)
        return False
    return r.returncode == 0


def _restart(container: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "restart", "-t", "10", container],
            capture_output=True,
            timeout=_RESTART_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("safe_update: restart failed for %s: %s", container, exc)
        return False
    return r.returncode == 0


def _retag(image_id: str, image_ref: str) -> bool:
    """Re-point ``image_ref`` (a tag) back to ``image_id`` for rollback."""
    try:
        r = subprocess.run(
            ["docker", "tag", image_id, image_ref],
            capture_output=True,
            timeout=_INSPECT_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("safe_update: retag failed (%s -> %s): %s", image_id, image_ref, exc)
        return False
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Snapshot-and-rollback gate — capture prior state before ANY mutation
# ---------------------------------------------------------------------------
#
# Reuses the safe_update_container pattern (capture -> mutate -> verify -> rollback)
# for every blind docker mutation in apply.py and checker.py.  A mutation that
# fails to restore leaves the system in unknown state; this gate makes it recoverable.
#
# Fail-safe rule: if snapshot capture itself fails, the mutation MUST NOT proceed.
# Never raises for an expected docker inspect failure — returns None to signal abort.

SnapshotState = dict[str, Any]  # {"image_id": str, "running": bool, "restart_count": int, ...}

_SNAPSHOT_INSPECT_TIMEOUT_S = 10


def _capture_container_state(container: str) -> SnapshotState | None:
    """Return the container's current image id, running status, and restart count.

    Returns None on any failure — the caller MUST abort the mutation (fail-safe).
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{json .}}", container],
            capture_output=True,
            text=True,
            timeout=_SNAPSHOT_INSPECT_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("snapshot: inspect failed for %s: %s", container, exc)
        return None
    if r.returncode != 0:
        return None
    try:
        state = json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("snapshot: inspect output parse failed for %s: %s", container, exc)
        return None
    return {
        "image_id": state.get("Image", ""),
        "running": state.get("State", {}).get("Running", False),
        "restart_count": state.get("State", {}).get("RestartCount", 0),
        "container_name": container,
    }


def _rollback_to_state(container: str, prior: SnapshotState) -> None:
    """Best-effort rollback: if the container was running before the mutation but
    isn't running now, attempt to start it. Logs at WARNING on failure but never
    raises — the original exception already propagates."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=_SNAPSHOT_INSPECT_TIMEOUT_S,
        )
        currently_running = r.returncode == 0 and r.stdout.strip() == "true"
    except (subprocess.SubprocessError, OSError):
        currently_running = False

    if prior.get("running") and not currently_running:
        log.warning(
            "snapshot rollback: %s was running before mutation, not running now — attempting start",
            container,
        )
        try:
            subprocess.run(
                ["docker", "start", container],
                capture_output=True,
                timeout=_RESTART_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("snapshot rollback: start failed for %s: %s", container, exc)


@contextmanager
def snapshot_container(container: str) -> Iterator[dict[str, Any]]:
    """Context manager: capture container state before a mutation, roll back on exception.

    Fail-safe: if snapshot capture itself fails, raises RuntimeError so the caller
    does NOT proceed with the mutation.

    On context exit with an exception, ``_rollback_to_state`` is called to attempt
    restoring the container to its prior running state. The original exception
    always propagates through.
    """
    prior = _capture_container_state(container)
    if prior is None:
        raise RuntimeError(f"Snapshot capture failed for {container}: cannot proceed with mutation")
    try:
        yield prior
    except Exception:
        _rollback_to_state(container, prior)
        raise


def safe_update_container(container: str, image_ref: str) -> SafeUpdateResult:
    """Pull ``image_ref``, restart ``container``, verify, and roll back on failure.

    Args:
        container: the container name (the app key).
        image_ref: the fully-qualified ``image:tag`` to pull and run.

    Returns a result dict the caller logs/records.  ``ok`` is True only when the
    container is verified healthy on the NEW image.  On a failed verify the
    function attempts to restore the PRIOR image and reports ``rolled_back``.
    Never raises for an expected docker error.
    """
    prior_image_id = _current_image_id(container)

    if not _pull(image_ref):
        return {
            "ok": False,
            "message": f"safe-update: pull failed for {image_ref}; container untouched",
            "rolled_back": False,
        }

    if not _restart(container):
        return {
            "ok": False,
            "message": f"safe-update: restart failed for {container} after pull",
            "rolled_back": False,
        }

    healthy, summary = verify_container_healthy(container)
    if healthy:
        log.info("safe_update: %s updated to %s and verified healthy", container, image_ref)
        return {
            "ok": True,
            "message": f"safe-update: {container} updated to {image_ref}; {summary}",
            "rolled_back": False,
        }

    # New image did not become healthy — roll back to the captured image id.
    if prior_image_id and _retag(prior_image_id, image_ref) and _restart(container):
        re_healthy, re_summary = verify_container_healthy(container)
        if re_healthy:
            log.warning(
                "safe_update: %s rolled back to prior image after failed update (%s)",
                container,
                summary,
            )
            return {
                "ok": False,
                "message": (
                    f"safe-update: {container} update failed verify ({summary}); "
                    f"rolled back to prior image — {re_summary}"
                ),
                "rolled_back": True,
            }
        return {
            "ok": False,
            "message": (
                f"safe-update: {container} update failed AND rollback did not verify "
                f"({re_summary}) — manual intervention required"
            ),
            "rolled_back": True,
        }

    return {
        "ok": False,
        "message": (
            f"safe-update: {container} update failed verify ({summary}); "
            "could not roll back (no prior image id captured or retag/restart failed)"
        ),
        "rolled_back": False,
    }


# ---------------------------------------------------------------------------
# Autonomy gate — propose vs. execute for an AGENT-INITIATED SAFE-update.
# ---------------------------------------------------------------------------


def _auto_update_pref_on(app_key: str) -> bool:
    """Return True iff the per-app update preference opts INTO auto-update.

    Reuses the same preference store the updates API writes
    (``backend.api.updates``): a container auto-updates only when it is NOT
    ``notify_only`` and NOT ``pinned``.  Defaults (notify_only=True, and SLOP's
    own container pinned) therefore yield False — the safe default.  Any read
    failure is treated as "pref off" (fail-closed).
    """
    try:
        from backend.api.updates import _default_pref, _load_prefs
        from backend.core.state import StateDB

        with StateDB() as db:
            prefs = _load_prefs(db)
        pref = prefs.get(app_key, _default_pref(app_key))
    except Exception as exc:  # fail-closed: unknown pref => do not auto-update
        log.debug("safe_update: auto-update pref read failed for %s: %s", app_key, exc)
        return False
    return not pref.get("notify_only", True) and not pref.get("pinned", False)


def evaluate_update_gate(app_key: str, level: OperationalLevel) -> GateDecision:
    """Decide whether an agent-initiated SAFE-update may EXECUTE for ``app_key``.

    The gate is deliberately conservative — a SAFE image update restarts the
    container and is disruptive, so it must never fire silently:

      * ADVISORY / SUPERVISED (default) → PROPOSE only (``execute=False``).
      * AUTONOMOUS **and** the per-app auto-update preference is on → EXECUTE.
      * AUTONOMOUS but the per-app preference is off → PROPOSE only.

    Returns ``{"execute": bool, "reason": str}``.
    """
    if level is not OperationalLevel.AUTONOMOUS:
        return {
            "execute": False,
            "reason": f"operational level {level.value}: SAFE-update proposed, not executed",
        }
    if not _auto_update_pref_on(app_key):
        return {
            "execute": False,
            "reason": (
                f"AUTONOMOUS but per-app auto-update preference off for {app_key}: "
                "SAFE-update proposed, not executed"
            ),
        }
    return {
        "execute": True,
        "reason": f"AUTONOMOUS and per-app auto-update preference on for {app_key}",
    }


__all__ = [
    "GateDecision",
    "SafeUpdateResult",
    "SnapshotState",
    "_capture_container_state",
    "_rollback_to_state",
    "evaluate_update_gate",
    "safe_update_container",
    "snapshot_container",
]
