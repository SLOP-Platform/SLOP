"""backend/api/apps_validate.py

Community/user manifest sanitization (id=472), extracted from apps.py
(#1302 linecount drain).

``sanitize_manifest`` is a pure function applied to user/community-sourced
manifest dicts BEFORE they are processed into compose fragments or persisted to
disk. apps.py re-imports it for the custom/community install path; the allow-list
and validation constants are private to this module.
"""

from __future__ import annotations

import re
from typing import Any

# Allow-list of top-level manifest keys that a community manifest may set.
# Any other key is silently stripped (defence in depth against future fields
# being injected via crafted manifests).
_MANIFEST_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "key",
        "display_name",
        "description",
        "category",
        "tier",
        "service_type",
        "linuxserver",
        "image",
        "image_tag",
        "start_grace_s",
        "ports",
        "volumes",
        "traefik",
        "health",
        "tags",
        "env",
        "extra_config",
        "web_port",
        "source",
        "source_url",
        "post_deploy",
        "wiring",
        "companions",
    }
)

# Shell metacharacters and command-injection candidates to strip from string values.
_SHELL_META_RE = re.compile(r"[;&|`$><\\\n\r]")

# Path traversal sequences to strip from string values.
_PATH_TRAVERSAL_RE = re.compile(r"\.\.[/\\]|^~")

# Allowed post_deploy step types for community manifests.
_ALLOWED_POST_DEPLOY_TYPES: frozenset[str] = frozenset({"wire", "wait_healthy", "api_ready"})

# Safe character pattern for companion image references.
_COMPANION_IMAGE_RE = re.compile(r"^[a-zA-Z0-9._/:-]+$")


def _sanitize_value(val: Any) -> Any:
    """Recursively strip shell metacharacters + path-traversal from string values."""
    if isinstance(val, str):
        val = _SHELL_META_RE.sub("", val)
        val = _PATH_TRAVERSAL_RE.sub("", val)
        return val
    if isinstance(val, dict):
        return {k: _sanitize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_sanitize_value(item) for item in val]
    return val  # int, float, bool, None — pass through unchanged


def _validate_post_deploy_types(result: dict[str, Any]) -> None:
    """Reject post_deploy step types not in the community allow-list."""
    if "post_deploy" not in result:
        return
    for step in result["post_deploy"]:
        if isinstance(step, dict):
            step_type = step.get("type", "")
            if step_type and step_type not in _ALLOWED_POST_DEPLOY_TYPES:
                raise ValueError(
                    f"post_deploy step type {step_type!r} is not allowed in community manifests "
                    f"(allowed: {', '.join(sorted(_ALLOWED_POST_DEPLOY_TYPES))})"
                )


def _validate_wiring(result: dict[str, Any]) -> None:
    """Require type/to on connects_to and type/from on accepts wiring entries."""
    if "wiring" not in result:
        return
    wiring = result["wiring"]
    if not isinstance(wiring, dict):
        return
    for entry in wiring.get("connects_to", []):
        if isinstance(entry, dict) and ("type" not in entry or "to" not in entry):
            raise ValueError(
                f"wiring.connects_to entry missing required fields 'type' and 'to': {entry!r}"
            )
    for entry in wiring.get("accepts", []):
        if isinstance(entry, dict) and ("type" not in entry or "from" not in entry):
            raise ValueError(
                f"wiring.accepts entry missing required fields 'type' and 'from': {entry!r}"
            )


def _validate_companions(result: dict[str, Any]) -> None:
    """Reject companion image references containing unsafe characters."""
    if "companions" not in result:
        return
    for companion in result["companions"]:
        if isinstance(companion, dict):
            image = companion.get("image", "")
            if image and not _COMPANION_IMAGE_RE.match(image):
                raise ValueError(f"companion image {image!r} contains invalid characters")


def sanitize_manifest(manifest_data: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a community/user-sourced manifest dict.

    Pure function (no side effects) — safe to call repeatedly.

    Steps applied:
      1. Strip keys not in _MANIFEST_ALLOWED_KEYS.
      2. In all remaining string values (recursively), remove shell
         metacharacters (;&|`$><\\\\n\\r) and path traversal sequences
         (../ or starting with ~).
      3. Validate post_deploy step types, wiring entries, and companion images.

    Args:
        manifest_data: Raw manifest dict from user input.

    Returns:
        Sanitized copy of the dict (input is not mutated).
    """
    result = {
        k: _sanitize_value(v) for k, v in manifest_data.items() if k in _MANIFEST_ALLOWED_KEYS
    }
    _validate_post_deploy_types(result)
    _validate_wiring(result)
    _validate_companions(result)
    return result
