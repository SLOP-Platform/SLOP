"""backend/agent/host_audit.py — host substrate probes.

Five GROUND probes that observe OS-level physics and emit frozen-verdict
:class:`Finding` objects.  No remediation is wired — alert-only.

Probes:
  1. disk_fill          — statvfs used% on /, /var/lib/docker, data_dir
  2. inode_exhaustion   — statvfs free-inode ratio on same 3 paths
  3. memory_pressure    — /proc/meminfo MemAvailable
  4. oom_psi            — /proc/pressure/memory some avg60
  5. clock_skew         — timedatectl NTPSynchronized

INDETERMINATE when a ground source is unreachable; never a silent VERIFIED.
Each probe is independently guarded so one failure does not suppress others.
Detail fields contain no hostnames, usernames, or IP addresses.
"""

from __future__ import annotations

import os
import subprocess

from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)

_PHYSICS_DISK = "os.statvfs on /, /var/lib/docker, data_dir"
_PHYSICS_INODE = "os.statvfs f_favail/f_files on /, /var/lib/docker, data_dir"
_PHYSICS_MEM = "/proc/meminfo MemAvailable"
_PHYSICS_PSI = "/proc/pressure/memory some avg60"
_PHYSICS_CLOCK = "timedatectl show NTPSynchronized"

_PROBE_PATHS = ["/", "/var/lib/docker"]


def _get_probe_paths() -> list[str]:
    """Return the 3 probe paths; data_dir resolved at call-time."""
    try:
        from backend.core.config import config as _cfg

        return ["/", "/var/lib/docker", str(_cfg.data_dir)]
    except Exception:
        return ["/", "/var/lib/docker"]


def _probe_disk_fill() -> Finding:
    """GROUND source 1: disk used% on /, /var/lib/docker, data_dir."""
    paths = _get_probe_paths()
    worst_pct = 0
    worst_path = "/"
    indeterminate_paths: list[str] = []

    for p in paths:
        try:
            s = os.statvfs(p)
            total = s.f_blocks * s.f_frsize
            free = s.f_bavail * s.f_frsize
            used_pct = int((total - free) / total * 100) if total > 0 else 0
            if used_pct > worst_pct:
                worst_pct = used_pct
                worst_path = p
        except OSError:
            indeterminate_paths.append(p)

    if indeterminate_paths and worst_pct == 0:
        return Finding(
            id="host.disk_fill",
            physics=_PHYSICS_DISK,
            verdict=Verdict.INDETERMINATE,
            summary=f"disk probe: paths unavailable: {indeterminate_paths}",
            detail="",
        )
    if worst_pct >= 90:
        return Finding(
            id="host.disk_fill",
            physics=_PHYSICS_DISK,
            verdict=Verdict.DRIFT,
            summary=f"disk CRIT: {worst_path} {worst_pct}% used",
            detail=f"used={worst_pct}% path={worst_path}",
        )
    if worst_pct >= 80:
        return Finding(
            id="host.disk_fill",
            physics=_PHYSICS_DISK,
            verdict=Verdict.DRIFT,
            summary=f"disk WARN: {worst_path} {worst_pct}% used",
            detail=f"used={worst_pct}% path={worst_path}",
        )
    return Finding(
        id="host.disk_fill",
        physics=_PHYSICS_DISK,
        verdict=Verdict.VERIFIED,
        summary=f"disk ok: worst {worst_pct}% at {worst_path}",
        detail=f"used={worst_pct}% path={worst_path}",
    )


def _probe_inode_exhaustion() -> Finding:
    """GROUND source 2: free-inode ratio on /, /var/lib/docker, data_dir."""
    paths = _get_probe_paths()
    worst_free_pct = 100.0
    worst_path = "/"
    indeterminate_paths: list[str] = []
    any_measured = False

    for p in paths:
        try:
            s = os.statvfs(p)
            if s.f_files == 0:
                # tmpfs or filesystem with no inode limit — skip
                continue
            free_pct = s.f_favail / s.f_files * 100
            any_measured = True
            if free_pct < worst_free_pct:
                worst_free_pct = free_pct
                worst_path = p
        except OSError:
            indeterminate_paths.append(p)

    if indeterminate_paths and not any_measured:
        return Finding(
            id="host.inode_exhaustion",
            physics=_PHYSICS_INODE,
            verdict=Verdict.INDETERMINATE,
            summary=f"inode probe: paths unavailable: {indeterminate_paths}",
            detail="",
        )

    if not any_measured:
        # All paths skipped (no inode limits) — treat as ok
        return Finding(
            id="host.inode_exhaustion",
            physics=_PHYSICS_INODE,
            verdict=Verdict.VERIFIED,
            summary="inode ok: no inode-limited filesystems found",
            detail="",
        )

    worst_pct_int = int(worst_free_pct)
    if worst_free_pct < 2.0:
        return Finding(
            id="host.inode_exhaustion",
            physics=_PHYSICS_INODE,
            verdict=Verdict.DRIFT,
            summary=f"inode CRIT: {worst_path} only {worst_pct_int}% inodes free",
            detail=f"free_pct={worst_free_pct:.1f}% path={worst_path}",
        )
    if worst_free_pct < 10.0:
        return Finding(
            id="host.inode_exhaustion",
            physics=_PHYSICS_INODE,
            verdict=Verdict.DRIFT,
            summary=f"inode WARN: {worst_path} only {worst_pct_int}% inodes free",
            detail=f"free_pct={worst_free_pct:.1f}% path={worst_path}",
        )
    return Finding(
        id="host.inode_exhaustion",
        physics=_PHYSICS_INODE,
        verdict=Verdict.VERIFIED,
        summary=f"inode ok: worst {worst_pct_int}% free at {worst_path}",
        detail=f"free_pct={worst_free_pct:.1f}% path={worst_path}",
    )


def _probe_memory_pressure() -> Finding:
    """GROUND source 3: MemAvailable from /proc/meminfo."""
    _WARN_KB = 512 * 1024  # 512 MB
    _CRIT_KB = 200 * 1024  # 200 MB

    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    avail_kb = int(parts[1])
                    break
            else:
                return Finding(
                    id="host.memory_pressure",
                    physics=_PHYSICS_MEM,
                    verdict=Verdict.INDETERMINATE,
                    summary="MemAvailable not found in /proc/meminfo",
                    detail="",
                )
    except OSError:
        return Finding(
            id="host.memory_pressure",
            physics=_PHYSICS_MEM,
            verdict=Verdict.INDETERMINATE,
            summary="memory probe: /proc/meminfo unavailable",
            detail="",
        )

    avail_mb = avail_kb // 1024
    if avail_kb < _CRIT_KB:
        return Finding(
            id="host.memory_pressure",
            physics=_PHYSICS_MEM,
            verdict=Verdict.DRIFT,
            summary=f"memory CRIT: only {avail_mb} MB available",
            detail=f"avail_kb={avail_kb}",
        )
    if avail_kb < _WARN_KB:
        return Finding(
            id="host.memory_pressure",
            physics=_PHYSICS_MEM,
            verdict=Verdict.DRIFT,
            summary=f"memory WARN: only {avail_mb} MB available",
            detail=f"avail_kb={avail_kb}",
        )
    return Finding(
        id="host.memory_pressure",
        physics=_PHYSICS_MEM,
        verdict=Verdict.VERIFIED,
        summary=f"memory ok: {avail_mb} MB available",
        detail=f"avail_kb={avail_kb}",
    )


def _probe_oom_psi() -> Finding:
    """GROUND source 4: PSI some avg60 from /proc/pressure/memory."""
    try:
        with open("/proc/pressure/memory") as fh:
            content = fh.read()
    except OSError:
        # Kernel may not support PSI — INDETERMINATE, not DRIFT
        return Finding(
            id="host.oom_psi",
            physics=_PHYSICS_PSI,
            verdict=Verdict.INDETERMINATE,
            summary="PSI probe: /proc/pressure/memory unavailable (kernel may lack PSI support)",
            detail="",
        )

    some_avg60: float | None = None
    for line in content.splitlines():
        if line.startswith("some "):
            for token in line.split():
                if token.startswith("avg60="):
                    try:
                        some_avg60 = float(token[len("avg60=") :])
                    except ValueError:
                        pass
            break

    if some_avg60 is None:
        return Finding(
            id="host.oom_psi",
            physics=_PHYSICS_PSI,
            verdict=Verdict.INDETERMINATE,
            summary="PSI probe: could not parse some avg60 from /proc/pressure/memory",
            detail="",
        )

    if some_avg60 > 20.0:
        return Finding(
            id="host.oom_psi",
            physics=_PHYSICS_PSI,
            verdict=Verdict.DRIFT,
            summary=f"PSI WARN: memory some avg60={some_avg60:.2f} (> 20.0)",
            detail=f"some_avg60={some_avg60:.2f}",
        )
    return Finding(
        id="host.oom_psi",
        physics=_PHYSICS_PSI,
        verdict=Verdict.VERIFIED,
        summary=f"PSI ok: memory some avg60={some_avg60:.2f}",
        detail=f"some_avg60={some_avg60:.2f}",
    )


def _probe_clock_skew() -> Finding:
    """GROUND source 5: NTP synchronization via timedatectl."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        output = result.stdout.strip()
    except FileNotFoundError:
        return Finding(
            id="host.clock_skew",
            physics=_PHYSICS_CLOCK,
            verdict=Verdict.INDETERMINATE,
            summary="clock probe: timedatectl not found",
            detail="",
        )
    except Exception:
        return Finding(
            id="host.clock_skew",
            physics=_PHYSICS_CLOCK,
            verdict=Verdict.INDETERMINATE,
            summary="clock probe: timedatectl call failed",
            detail="",
        )

    if "NTPSynchronized=no" in output:
        return Finding(
            id="host.clock_skew",
            physics=_PHYSICS_CLOCK,
            verdict=Verdict.DRIFT,
            summary="clock WARN: NTP not synchronized",
            detail="NTPSynchronized=no",
        )
    if "NTPSynchronized=yes" in output:
        return Finding(
            id="host.clock_skew",
            physics=_PHYSICS_CLOCK,
            verdict=Verdict.VERIFIED,
            summary="clock ok: NTP synchronized",
            detail="NTPSynchronized=yes",
        )
    # Output present but neither yes nor no — INDETERMINATE
    return Finding(
        id="host.clock_skew",
        physics=_PHYSICS_CLOCK,
        verdict=Verdict.INDETERMINATE,
        summary="clock probe: NTPSynchronized value unrecognised",
        detail="",
    )


def reconcile_host() -> list[Finding]:
    """The host substrate GROUND reconciler.

    Returns one :class:`Finding` per ground source.  Each probe is
    independently guarded so one unreachable source yields its own
    INDETERMINATE without suppressing the others.  Reads no docs.
    """
    probes = [
        ("host.disk_fill", _probe_disk_fill),
        ("host.inode_exhaustion", _probe_inode_exhaustion),
        ("host.memory_pressure", _probe_memory_pressure),
        ("host.oom_psi", _probe_oom_psi),
        ("host.clock_skew", _probe_clock_skew),
    ]
    findings: list[Finding] = []
    for finding_id, probe_fn in probes:
        try:
            findings.append(probe_fn())
        except Exception as exc:
            log.warning("host_audit probe %s failed unexpectedly: %s", finding_id, exc)
            findings.append(
                Finding(
                    id=finding_id,
                    physics="host substrate probe",
                    verdict=Verdict.INDETERMINATE,
                    summary=f"probe raised unexpected exception: {type(exc).__name__}",
                    detail="",
                )
            )
    return findings


__all__ = ["reconcile_host"]
