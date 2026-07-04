"""backend/core/sqlbuild.py — validated SQL statement builders.

Centralizes the ``UPDATE ... SET`` clause construction that was previously
duplicated across ``StateDB`` (backend/core/state.py) and the LLM model
registry (backend/core/llm_router.py), each carrying its own per-site
``# noqa: S608 # nosec B608`` suppression (#925 fix 2).

The duplication forced the same trust argument ("columns come from a hardcoded
allow-set, values are ?-bound") to be re-asserted at every call site. Here it is
*enforced* instead: ``table`` and every column key are validated against a strict
bare-identifier pattern, so no caller can inject SQL through a column name, and
all row values plus ``where_params`` are bound via ``?`` placeholders. That makes
the single remaining suppression in this module a genuine false-positive rather
than a trust-me annotation — and collapses 5 call-site suppressions into 1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# A bare SQL identifier: a column or table name with no quoting, whitespace, or
# punctuation that could carry an injection. SQLite identifiers in this codebase
# are all of this shape.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_identifier(name: str) -> str:
    """Return ``name`` if it is a safe bare SQL identifier, else raise ValueError."""
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def build_update(
    table: str,
    updates: Mapping[str, Any],
    where: str,
    where_params: Sequence[Any] = (),
) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterized ``UPDATE`` statement and its bound parameters.

    ``table`` and every key in ``updates`` MUST be a bare SQL identifier (each is
    validated); a non-identifier raises ``ValueError`` before any SQL is built.
    Every value in ``updates`` and every item in ``where_params`` is bound via a
    ``?`` placeholder. ``where`` is a caller-supplied literal clause
    (e.g. ``"id = 1"`` or ``"slot = ?"``) — keep it free of interpolated input.

    Returns ``(sql, params)`` ready to pass straight to ``cursor.execute``.

    Raises ``ValueError`` if ``updates`` is empty or any identifier is unsafe.
    """
    _check_identifier(table)
    if not updates:
        raise ValueError("build_update requires at least one column to update")
    cols = ", ".join(f"{_check_identifier(k)} = ?" for k in updates)
    # S608/B608 false-positive: `table` and every column in `cols` are validated
    # bare identifiers (see _check_identifier); all values + where_params are
    # ?-bound; `where` is a caller-side literal clause, never user input.
    sql = f"UPDATE {table} SET {cols} WHERE {where}"  # noqa: S608  # nosec B608
    return sql, (*updates.values(), *where_params)
