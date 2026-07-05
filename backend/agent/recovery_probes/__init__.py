"""backend/agent/recovery_probes — per-domain GROUND probe modules.

Each sub-module owns one logical probe domain:
  - mount      : bind-mount source path health
  - backup     : backup configured (advisory) + backup freshness
  - cert       : TLS certificate expiry
  - credential : auto_secrets presence and well-formedness in .env

All probe functions are re-exported here for convenience.
"""

from __future__ import annotations

from backend.agent.recovery_probes.backup import (
    _probe_backup_configured,
    _probe_backup_freshness,
    _probe_backup_schedule_overdue,
    _probe_backup_verify_result,
    _probe_custom_volume_verify_results,
    _probe_media_volume_index_declared,
    _probe_platform_backup_verify_result,
)
from backend.agent.recovery_probes.cert import (
    _probe_cert_expiry,
)
from backend.agent.recovery_probes.credential import (
    _probe_credential_validity,
)
from backend.agent.recovery_probes.mount import (
    _probe_mount_health,
)

__all__ = [
    "_probe_backup_configured",
    "_probe_backup_freshness",
    "_probe_backup_schedule_overdue",
    "_probe_backup_verify_result",
    "_probe_cert_expiry",
    "_probe_credential_validity",
    "_probe_custom_volume_verify_results",
    "_probe_media_volume_index_declared",
    "_probe_mount_health",
    "_probe_platform_backup_verify_result",
]
