"""Filesystem-path injection guard — one shared validator for user-influenced
path components.

Sibling of ``backend/core/url_guard.py`` (SSRF guard). User-controlled values
(catalog keys, proposal ids, filenames) that flow into a constructed filesystem
path must be validated at the construction seam, NOT patched per-site, so a value
like ``../../etc/passwd`` or ``foo/bar`` can never escape its intended base dir.

CodeQL class closed: ``py/path-injection``.

``safe_component(value)`` rejects a single path segment that contains a
separator, ``..``, a NUL, or characters outside a conservative charset, and
returns the value unchanged when safe (so it composes inline:
``base / f"{safe_component(key)}.yaml"``). A value that passes it cannot contain a
separator or ``..``, so it is contained-by-construction within its base dir — no
separate resolve-and-confirm step is needed for these single-segment sinks.
"""

from __future__ import annotations

import re

#: A conservative path-segment charset. Allows alnum plus ``._-`` (so a value may
#: carry an extension) but ``..`` is rejected explicitly below, and a leading char
#: must be alphanumeric (no leading dot/dash). Separators are never allowed.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_COMPONENT = 128


class PathNotAllowed(ValueError):
    """Raised when a path component / constructed path fails validation.
    Subclass of ValueError so existing ``except ValueError`` handlers keep working."""


def safe_component(value: str, *, field: str = "value") -> str:
    """Return ``value`` unchanged if it is a safe single path segment, else raise.

    Rejects: empty, over-length, embedded ``/`` or ``\\``, ``..`` (traversal),
    NUL bytes, and anything outside ``[A-Za-z0-9._-]`` / not alnum-led.
    """
    if (
        not value
        or len(value) > _MAX_COMPONENT
        or "/" in value
        or "\\" in value
        or ".." in value
        or "\x00" in value
        or not _SAFE_COMPONENT.match(value)
    ):
        raise PathNotAllowed(f"Unsafe {field}: {value!r}")
    return value
