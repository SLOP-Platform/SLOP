"""installer/_defaults.py — canonical default path constants for ADR 0013 §1.

This is the SINGLE canonical location permitted to contain the literal strings
'/opt/slop' and '/var/lib/slop' in the installer/ tree (ADR 0013
INV-1 / Core Rule 5.26). All other installer/ modules must import from here
rather than repeating the literals.

check_structural_antipatterns.py rule-005 allowlists this file.
"""

DEFAULT_INSTALL_DIR: str = "/opt/slop"
DEFAULT_DATA_DIR: str = "/var/lib/slop"
