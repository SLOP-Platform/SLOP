"""backend/manifests/loader_types.py — manifest schema dataclasses + validation vocab (#1302 split).

Extracted from ``loader.py`` (which grew past the 500-line ``production_code`` cap during the
CI-dead window, #1271) to separate the **leaf schema types** from the YAML→AppManifest parsing and
caching logic. These are the per-section dataclasses an ``AppManifest`` is composed of (ports,
volumes, wires, health checks …) plus the ``VALID_*`` value-sets the parser validates against.

They are pure leaves — only primitive fields, no dependency on ``AppManifest``, the parser, or any
other ``loader`` code — so this module has **no import of** ``loader`` → no import cycle. ``loader``
imports them all back and re-exports them (redundant-alias idiom), so every existing caller keeps
resolving unchanged: ``AppManifest``'s field annotations, the ``loader_parsers`` parse functions
that construct them (``from backend.manifests.loader import PortDef`` …), and the tests that import
``VolumeDef`` / ``DependencyDef`` from ``loader``. Pure move, no behaviour change.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortDef:
    internal: int
    protocol: str = "tcp"
    name: str = ""


@dataclass
class VolumeDef:
    host_path: str  # relative to config_root or absolute
    container_path: str
    readonly: bool = False
    prompt_key: str = ""  # non-empty when host_path is a sentinel "<prompt:{key}>"
    # backup_class (design §14): config → full backup (default + fail-safe for an untagged volume);
    # media → exclude the re-acquirable bytes, but the app MUST declare its library/index via
    # `backup_verify` (else recovery_audit DRIFTs); exclude → operator-declared throwaway, skipped.
    # An UNKNOWN value coerces to "config" (fail-safe backup — never silently exclude, constraint 5).
    backup_class: str = "config"


@dataclass
class InstallPromptDef:
    key: str
    label: str
    description: str
    prompt_type: str = "string"  # "path" | "string"
    required: bool = False
    default: str = ""


@dataclass
class WireDef:
    wire_type: str  # indexer | notification | library | ...
    peer: str  # key of the other app
    direction: str  # accepts | connects_to
    description: str = ""
    optional: bool = False


@dataclass
class PostDeployStep:
    step_type: str  # wait_healthy | api_ready | wire | custom
    timeout: int = 60
    path: str = ""  # for api_ready
    target: str = ""  # for wire
    wire_type: str = ""


@dataclass
class HealthCheckDef:
    name: str
    check_type: str  # http | tcp | process | custom
    path: str = ""
    expect_status: int = 200
    interval: int = 30
    port: int = 0  # for tcp checks: override port (defaults to app host_port)


@dataclass
class SelfHealDef:
    condition: str  # matches a health check name
    action: str  # restart | rewire | notify
    max_attempts: int = 3
    cooldown: int = 60


@dataclass
class GpuDef:
    optional: bool = True
    warn_if_absent: bool = True
    nvidia: bool = True
    amd: bool = False


@dataclass
class DependencyDef:
    postgres: bool = False
    redis: bool = False
    mariadb: bool = False
    apps: list[str] = field(default_factory=list)


VALID_CATEGORIES = {
    "arr",
    "media",
    "downloader",
    "requests",
    "tools",
    "ai",
    "monitoring",
    "productivity",
    "infra",
    "agent",  # reserved for tier-0 system components (SLOP Agent etc.)
}

VALID_STEP_TYPES = {"wait_healthy", "api_ready", "wire", "custom"}
VALID_CHECK_TYPES = {"http", "tcp", "process", "custom"}
VALID_HEAL_ACTIONS = {"restart", "rewire", "notify"}
VALID_WIRE_DIRECTIONS = {"accepts", "connects_to"}
