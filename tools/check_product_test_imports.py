#!/usr/bin/env python3
"""Check product tests don't import internal (dev-apparatus) modules.

Scan tests/*.py for test files containing @pytest.mark.product or
pytestmark = [...] that includes pytest.mark.product. Only those files
are checked. Files without the product marker are NOT scanned (they are
DEV-ONLY tests that stay in the private dev repo).

Forbidden prefixes: tools., backend.agent., backend.health.

Exit 0 if clean, exit 1 if violations found.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

FORBIDDEN_PREFIXES = ("tools.", "backend.agent.", "backend.health.")


def _has_product_marker(path: Path) -> bool:
    """Check if a test file has the @pytest.mark.product marker."""
    text = path.read_text()
    return bool(
        re.search(r'pytestmark\s*=\s*\[.*pytest\.mark\.product.*\]', text)
        or re.search(r'@pytest\.mark\.product', text)
    )


def _forbidden_import(node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            for prefix in FORBIDDEN_PREFIXES:
                if alias.name == prefix.rstrip(".") or alias.name.startswith(prefix):
                    return alias.name
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        for prefix in FORBIDDEN_PREFIXES:
            if module == prefix.rstrip(".") or module.startswith(prefix):
                return module
    return None


def check_file(path: Path) -> list[str]:
    violations: list[str] = []
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return violations
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            forbidden = _forbidden_import(node)
            if forbidden:
                violations.append(f"  {path.name}:{node.lineno}: imports {forbidden}")
    return violations


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    tests_dir = repo / "tests"
    if not tests_dir.is_dir():
        print("OK: no tests/ directory")
        return 0

    all_violations: list[str] = []
    product_count = 0
    for f in sorted(tests_dir.glob("test_*.py")):
        if not _has_product_marker(f):
            continue
        product_count += 1
        all_violations.extend(check_file(f))

    if all_violations:
        print(f"VIOLATIONS: product tests import forbidden internal modules ({len(all_violations)} hits)")
        for v in all_violations:
            print(v)
        return 1

    print(f"OK: {product_count} product test files — no forbidden imports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
