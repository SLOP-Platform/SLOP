"""backend/agent/taxonomy.py

Error classification taxonomy for the LLM agent pipeline.

Defines:
  ErrorClass — 10-value enum covering all observable Docker/compose failure
  modes surfaced through the install monitor.

  DETECTION_PATTERNS — ordered dict of regex patterns per class. Iteration
  order matches the authoritative detection priority from §4 of
  LLM-AGENT-DESIGN.md:
    IMAGE_PULL_FAIL → PORT_CONFLICT → EPERM_VOLUME → RESOURCE_EXHAUSTION →
    UNRESOLVED_PLACEHOLDER → MISSING_ENV_VAR → DEPENDENCY_DOWN →
    HEALTHCHECK_TIMEOUT → CRASH_LOOP → UNKNOWN

  UNKNOWN has an empty pattern list — it is the fallback and is never matched.

Usage:
    from backend.agent.taxonomy import ErrorClass, DETECTION_PATTERNS
"""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    """Ten canonical error classes for install/runtime failures.

    Using str as the mixin base means the enum value is directly usable
    as a string (e.g. ``error_class.value`` and ``str(error_class)`` are
    both ``'IMAGE_PULL_FAIL'``), which matches the TEXT column in SQLite.
    """

    IMAGE_PULL_FAIL = "IMAGE_PULL_FAIL"
    PORT_CONFLICT = "PORT_CONFLICT"
    EPERM_VOLUME = "EPERM_VOLUME"
    MISSING_ENV_VAR = "MISSING_ENV_VAR"
    UNRESOLVED_PLACEHOLDER = "UNRESOLVED_PLACEHOLDER"
    HEALTHCHECK_TIMEOUT = "HEALTHCHECK_TIMEOUT"
    CRASH_LOOP = "CRASH_LOOP"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    DEPENDENCY_DOWN = "DEPENDENCY_DOWN"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Detection patterns — iteration order defines priority.
# Each pattern is matched case-insensitively against the error text.
# The FIRST class whose ANY pattern matches wins.
# ---------------------------------------------------------------------------

DETECTION_PATTERNS: dict[ErrorClass, list[str]] = {
    # 1. Image pull failures — check before volume/permission errors
    ErrorClass.IMAGE_PULL_FAIL: [
        r"manifest unknown",
        r"pull access denied",
        r"not found: manifest",
    ],
    # 2. Port binding conflicts
    ErrorClass.PORT_CONFLICT: [
        r"bind: address already in use",
    ],
    # 3. Volume/filesystem permission errors
    ErrorClass.EPERM_VOLUME: [
        r"permission denied",
    ],
    # 4. Resource exhaustion — OOMKilled before CRASH_LOOP to avoid
    #    misclassifying OOM restarts as generic crash loops
    ErrorClass.RESOURCE_EXHAUSTION: [
        r"OOMKilled",
        r"ENOSPC",
        r"no space left",
        r"out of memory",
    ],
    # 5. Unresolved template placeholders (literal braces in env values)
    ErrorClass.UNRESOLVED_PLACEHOLDER: [
        r"\{[a-z_]+\}",
    ],
    # 6. Missing/unset environment variables
    ErrorClass.MISSING_ENV_VAR: [
        r"is required",
        r"not set",
        r"Environment variable .* not set",
    ],
    # 7. Upstream service/network unreachable
    ErrorClass.DEPENDENCY_DOWN: [
        r"Connection refused",
        r"no such host",
        r"dial tcp.*i/o timeout",
    ],
    # 8. Healthcheck failures (container up but unhealthy)
    ErrorClass.HEALTHCHECK_TIMEOUT: [
        r"unhealthy",
        r"health_status: unhealthy",
    ],
    # 9. Crash loops (restart_count-driven; OOMKilled handled above)
    ErrorClass.CRASH_LOOP: [
        r"restart_count",
        r"Restarting \(",
        r"OOMKilled",
    ],
    # 10. Fallback — never matched; always the classifier's last resort
    ErrorClass.UNKNOWN: [],
}
