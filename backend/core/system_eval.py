"""backend/core/system_eval.py

System host evaluation — measures available resources and determines
the maximum LLM model size that can be used without impacting the stack.

Run during:
  - Platform wizard (Step 0 — before preflight)
  - Every 6 hours via health scheduler
  - Before any GGUF model download
  - Before each LLM inference call (lightweight RAM check only)

All measurements are estimates. The UI clearly labels them as such.
"""

from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, cast

from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# RAM estimates per app category
# ---------------------------------------------------------------------------


# Conservative estimates in MB — real usage varies with library size and config
APP_RAM_MB: dict[str, int] = {
    # Infrastructure
    "traefik": 80,
    "tinyauth": 40,
    "authelia": 150,
    "authentik": 400,
    "oauth2-proxy": 50,
    "cloudflared": 60,
    "tailscale": 80,
    "headscale": 80,
    "netbird": 90,
    "zerotier": 70,
    "pangolin": 70,
    "nebula": 70,
    "gluetun": 50,
    "portainer": 200,
    "dockhand": 100,
    "dockge": 100,
    "komodo": 300,
    "homepage": 100,
    # Managed services
    "postgres": 150,
    "redis": 50,
    "mariadb": 150,
    # Media
    "plex": 1500,
    "jellyfin": 800,
    "sonarr": 300,
    "radarr": 300,
    "lidarr": 250,
    "readarr": 250,
    "bazarr": 200,
    "prowlarr": 200,
    "whisparr": 300,
    "mylar3": 200,
    "tdarr": 400,
    "fileflows": 300,
    "seerr": 250,
    "audiobookshelf": 250,
    "tautulli": 150,
    "kavita": 200,
    "booklore": 300,
    "midarr": 200,
    # Downloaders
    "qbittorrent": 150,
    "sabnzbd": 150,
    "nzbget": 100,
    "slskd": 200,
    # Tools
    "vaultwarden": 50,
    "freshrss": 100,
    "filebrowser": 50,
    "memos": 80,
    "silverbullet": 80,
    "donetick": 60,
    "vikunja": 150,
    "mealie": 200,
    "paperless_ngx": 400,
    "immich": 800,
    "affine": 400,
    "actual_budget": 80,
    "bookstack": 300,
    "gitea": 200,
    "n8n": 300,
    "yourls": 80,
    "koffan": 30,
    "faved": 50,
    "bentopdf": 80,
    "stirling_pdf": 200,
    "changedetection": 150,
    "syncthing": 150,
    "rclone": 100,
    "guacamole": 300,
    # Monitoring
    "glance": 80,
    "dozzle": 50,
    "beszel": 80,
    "netdata": 250,
    "uptime_kuma": 100,
    "grafana": 200,
    "prometheus": 200,
    "speedtest_tracker": 100,
    "umami": 200,
    "crowdsec": 100,
    "adguard_home": 100,
    "pi_hole": 100,
    # AI
    "ollama": 200,  # idle only — add model size during inference
    "llamacpp_server": 100,  # idle only — model loaded on demand
    # Other
    "ntfy": 30,
    "watchtower": 50,
    "ddns_updater": 30,
    "scrutiny": 100,
    "snapraid_ui": 80,
    "configarr": 200,
    "recyclarr": 100,
}

# SLOP itself
SLOP_OVERHEAD_MB = 150

# OS baseline (kernel + system services)
OS_BASELINE_MB = 512

# Safety buffer: keep this fraction free for spikes
SAFETY_BUFFER_FRACTION = 0.15

# LLM inference RAM requirements (model stays loaded during call)
LLM_MODEL_RAM_MB: dict[str, int] = {
    "smollm2:1.7b": 1200,
    "llama3.2:3b": 2500,
    "qwen2.5:3b": 2200,
    "phi4-mini": 3000,
    "gemma3:4b": 3200,
    "llama3.1:8b": 5000,  # ~4.7GB quantized
    "llama3.3:70b-instruct-q4_K_M": 42000,  # GPU only
}

# Headroom thresholds = model_ram + Ollama process + 10% buffer
# e.g. phi4-mini: 3000MB model + 200MB Ollama idle + 300MB buffer = 3500MB needed
LLM_TIERS: list[tuple[int, str, list[str]]] = [
    # (min_headroom_mb, tier_label, available_models)
    # headroom must cover: model_ram + 200MB ollama + 10% buffer
    (
        5500,
        "llama3.1:8b",
        ["smollm2:1.7b", "llama3.2:3b", "qwen2.5:3b", "phi4-mini", "llama3.1:8b"],
    ),
    (3500, "phi4-mini", ["smollm2:1.7b", "llama3.2:3b", "qwen2.5:3b", "phi4-mini"]),
    (2900, "llama3.2", ["smollm2:1.7b", "llama3.2:3b", "qwen2.5:3b"]),
    (1600, "smollm2", ["smollm2:1.7b"]),
    (0, "none", []),
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DiskInfo:
    path: str
    total_gb: float
    used_gb: float
    free_gb: float
    percent_used: float


@dataclass
class SystemProfile:
    # Hardware — required fields first (no default), then optional
    cpu_cores: int
    cpu_model: str
    total_ram_mb: int
    architecture: str
    # CPU capabilities (optional — detected at runtime)
    avx: bool = False
    avx2: bool = False
    avx512: bool = False
    # GPU (optional — not all systems have one)
    gpu_vendor: str | None = None
    gpu_name: str | None = None
    gpu_vram_mb: int = 0
    gpu_inference_capable: bool = False
    gpu_cuda_version: str | None = None

    # Current usage (required, measured at runtime)
    used_ram_mb: int = 0
    free_ram_mb: int = 0
    docker_container_ram_mb: int = 0

    # Storage
    disks: list[DiskInfo] = field(default_factory=list)

    # Stack projection
    selected_apps: list[str] = field(default_factory=list)
    estimated_stack_ram_mb: int = 0
    headroom_ram_mb: int = 0

    # LLM recommendation
    recommended_model: str = ""
    available_models: list[str] = field(default_factory=list)
    llm_warning: str | None = None

    # OS / environment
    os_distro: str = ""  # "Ubuntu", "Debian", "Rocky", etc.
    os_version: str = ""  # "24.04", "12", etc.
    os_arch: str = ""  # "x86_64", "arm64"
    kernel_version: str = ""  # "6.8.0-51-generic"
    # Docker
    docker_version: str = ""  # engine version
    docker_api_version: str = ""  # API version
    compose_version: str = ""  # compose plugin version
    containers_running: int = 0  # count of running containers
    # User / environment
    puid: int = 1000  # detected file-owner UID
    pgid: int = 1000  # detected file-owner GID
    puid_username: str = ""  # username for that UID
    timezone: str = ""  # "America/Los_Angeles"
    server_ip: str = ""  # primary LAN IP
    # Timestamp
    measured_at: int = 0

    @property
    def total_ram_gb(self) -> float:
        return self.total_ram_mb / 1024

    @property
    def available_ram_gb(self) -> float:
        """RAM headroom available for new workloads (not reserved by OS or containers)."""
        return max(0, self.headroom_ram_mb) / 1024

    @property
    def used_ram_gb(self) -> float:
        return self.used_ram_mb / 1024


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def read_meminfo() -> dict[str, int]:
    """Parse /proc/meminfo into a dict of key → kB values."""
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        info[key] = int(parts[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return info


def read_cpuinfo() -> tuple[int, str, dict[str, Any]]:
    """Return (core_count, model_name, avx_flags) from one /proc/cpuinfo read."""
    cores = os.cpu_count() or 1
    model = "Unknown"
    avx = {"avx": False, "avx2": False, "avx512": False}
    try:
        text = Path("/proc/cpuinfo").read_text()
        max_mhz = 0.0
        for line in text.splitlines():
            if line.startswith("model name") and model == "Unknown":
                model = line.split(":", 1)[1].strip()
            elif line.startswith("cpu MHz"):
                try:
                    mhz = float(line.split(":", 1)[1].strip())
                    if mhz > max_mhz:
                        max_mhz = mhz
                except ValueError:
                    pass
        # Append frequency to model name if not already present and detected
        if max_mhz > 0 and "GHz" not in model and "MHz" not in model:
            ghz = max_mhz / 1000
            model = f"{model} @ {ghz:.2f}GHz"
        # Extract AVX flags from the same read
        avx["avx"] = " avx " in text
        avx["avx2"] = "avx2" in text
        avx["avx512"] = "avx512" in text
    except OSError:
        pass
    return cores, model, avx


def disk_usage(path: str) -> DiskInfo | None:
    """Get disk usage for a path."""
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        total_gb = total / 1_073_741_824
        used_gb = used / 1_073_741_824
        free_gb = free / 1_073_741_824
        pct = (used / total * 100) if total > 0 else 0
        return DiskInfo(
            path=path,
            total_gb=round(total_gb, 1),
            used_gb=round(used_gb, 1),
            free_gb=round(free_gb, 1),
            percent_used=round(pct, 1),
        )
    except (OSError, ZeroDivisionError):
        return None


def docker_ram_usage_mb() -> int:
    """Estimate total RAM used by running Docker containers via /proc."""
    try:
        import subprocess

        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        total_mb: float = 0
        for line in result.stdout.splitlines():
            # Format: "123.4MiB / 15.6GiB"
            used = line.split("/")[0].strip()
            if "GiB" in used:
                total_mb += float(used.replace("GiB", "").strip()) * 1024
            elif "MiB" in used:
                total_mb += float(used.replace("MiB", "").strip())
            elif "KiB" in used:
                total_mb += float(used.replace("KiB", "").strip()) / 1024
        return int(total_mb)
    except Exception:
        return 0


def estimate_stack_ram(app_keys: list[str]) -> int:
    """Estimate total RAM in MB for a list of installed apps."""
    total = SLOP_OVERHEAD_MB + OS_BASELINE_MB
    total += APP_RAM_MB.get("postgres", 150)  # managed services always running
    total += APP_RAM_MB.get("redis", 50)
    for key in app_keys:
        total += APP_RAM_MB.get(key, 200)  # default 200MB for unknown apps
    return total


def recommend_llm(
    headroom_mb: int, ram_bandwidth_gbs: float = 0.0
) -> tuple[str, list[str], str | None]:
    """Return (recommended_model, available_models, warning) for given RAM headroom.

    Args:
        headroom_mb: available RAM after stack + safety buffer (must cover model RAM too)
        ram_bandwidth_gbs: measured RAM bandwidth in GB/s (0 = unknown, skip speed gate)
    """
    # Speed gate: RAM bandwidth < 15 GB/s = DDR3/slow system.
    # SmolLM2 (1.2GB) at 15 GB/s = ~12 tok/s (acceptable).
    # Phi4-mini (3GB) at 15 GB/s = ~5 tok/s (marginal but usable).
    # If bandwidth < 8 GB/s, cap at smollm2 even if RAM fits larger models.
    slow_ram = ram_bandwidth_gbs > 0 and ram_bandwidth_gbs < 8.0
    very_slow = ram_bandwidth_gbs > 0 and ram_bandwidth_gbs < 4.0

    for min_mb, tier, models in LLM_TIERS:
        if headroom_mb >= min_mb:
            if tier == "none":
                return (
                    "",
                    [],
                    f"Only {headroom_mb}MB of RAM headroom available. "
                    f"LLM agent requires at least 1.6GB (model + Ollama overhead). "
                    f"Reduce running apps or add RAM.",
                )
            warning = None

            # Apply speed gate
            if very_slow and tier != "smollm2":
                return (
                    "smollm2:1.7b",
                    ["smollm2:1.7b"],
                    f"RAM bandwidth is very low ({ram_bandwidth_gbs:.1f} GB/s). "
                    f"Only SmolLM2 will run at an acceptable speed on this system.",
                )
            if slow_ram and tier in ("phi4-mini", "llama3.2", "llama3.1:8b"):
                # Downgrade recommendation to qwen2.5:3b
                avail = ["smollm2:1.7b", "llama3.2:3b", "qwen2.5:3b"]
                return (
                    "qwen2.5:3b",
                    avail,
                    f"RAM bandwidth ({ram_bandwidth_gbs:.1f} GB/s) is below DDR4 speeds. "
                    f"Larger models will run slowly. Qwen 2.5 3B is the best practical choice.",
                )

            if tier == "smollm2":
                warning = (
                    "Only SmolLM2 1.7B fits safely in available RAM. "
                    "Diagnostic quality will be limited — acceptable for triage."
                )
            return tier, models, warning

    return "", [], f"Insufficient RAM for LLM ({headroom_mb}MB available — need ≥1.6GB)."


def detect_avx() -> dict[str, Any]:
    """Detect CPU AVX/AVX2/AVX512 — reads /proc/cpuinfo once via read_cpuinfo()."""
    _, _, avx = read_cpuinfo()
    return avx


def detect_gpu() -> dict[str, Any]:
    """Detect GPU vendor, name, and VRAM."""
    import subprocess as _sp

    result: dict[str, str | int | bool | None] = {
        "vendor": None,
        "name": None,
        "vram_mb": 0,
        "cuda_version": None,
        "inference_capable": False,
    }
    try:
        out = _sp.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            parts = [p.strip() for p in out.stdout.strip().split(",")]
            result.update(
                {
                    "vendor": "nvidia",
                    "name": parts[0],
                    "vram_mb": int(parts[1]) if len(parts) > 1 else 0,
                    "inference_capable": True,
                }
            )
            return result
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    try:
        import subprocess as _sp2

        lspci = _sp2.run(["lspci"], capture_output=True, text=True, timeout=5)
        if lspci.returncode == 0:
            for line in lspci.stdout.splitlines():
                if "VGA" in line or "3D" in line:
                    result["name"] = line.split(":")[-1].strip()[:60]
                    if "Intel" in line:
                        result["vendor"] = "intel"
                    elif "AMD" in line or "ATI" in line:
                        result["vendor"] = "amd"
                    elif "NVIDIA" in line:
                        result["vendor"] = "nvidia"
                    break
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    return result


def detect_os() -> dict[str, Any]:
    """Detect Linux distro, version, kernel, and architecture."""
    result = {
        "distro": "",
        "version": "",
        "arch": platform.machine(),
        "kernel": platform.release(),
    }
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("NAME="):
                    result["distro"] = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    result["version"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        try:
            result["distro"] = platform.system()
            result["version"] = platform.release()
        except Exception as e:
            log.debug("system probe skipped: %s", e)
    return result


def detect_docker_version() -> dict[str, Any]:
    """Get Docker engine and Compose plugin versions."""
    import subprocess as _sp

    result = {"engine": "", "api": "", "compose": ""}
    try:
        r = _sp.run(
            ["docker", "version", "--format", "{{.Server.Version}}\t{{.Server.APIVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split("\t")
            result["engine"] = parts[0] if parts else ""
            result["api"] = parts[1] if len(parts) > 1 else ""
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    try:
        r2 = _sp.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r2.returncode == 0:
            result["compose"] = r2.stdout.strip()
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    return result


def detect_running_containers() -> int:
    """Count running containers. Fast path: docker ps -q."""
    import subprocess as _sp

    try:
        r = _sp.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return len([line for line in r.stdout.splitlines() if line.strip()])
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    return 0


def detect_timezone() -> str:
    """Detect system timezone using multiple methods.

    Ordered fastest-first: file reads before subprocess spawns.
    """
    # Method 1: /etc/timezone (Debian/Ubuntu, Alpine) — 0.3ms, most common
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz and "/" in tz:
            return tz
    except OSError:
        pass
    # Method 2: /etc/localtime symlink — 0ms, works on Fedora/Arch/Rocky
    try:
        lt = os.readlink("/etc/localtime")
        if "zoneinfo/" in lt:
            return lt.split("zoneinfo/", 1)[1]
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    # Method 3: TZ env var (containers often set this)
    if tz_env := os.environ.get("TZ", ""):
        return tz_env
    # Method 4: timedatectl (systemd, ~17ms subprocess spawn — last resort)
    try:
        import subprocess as _sp

        r = _sp.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    return "UTC"


def _owner_from_paths(paths: list[str]) -> tuple[int, int, str] | None:
    """Strategy 1: stat each candidate path; first owner with UID≥1000 wins."""
    import pwd as _pwd

    for path in paths:
        try:
            st = os.stat(path)
            uid = st.st_uid
            gid = st.st_gid
        except Exception as e:
            # unreadable candidate path: log and try the next one
            log.debug("owner stat skipped for %s: %s", path, e)
            continue
        if uid >= 1000:
            try:
                name = _pwd.getpwuid(uid).pw_name
            except KeyError:
                name = str(uid)
            return uid, gid, name
    return None


def _owner_from_etc_passwd() -> tuple[int, int, str] | None:
    """Strategy 2: scan /etc/passwd for the first 'human' user (UID 1000-59999).
    Prefer entries whose home directory actually exists."""
    try:
        with open("/etc/passwd") as f:
            entries: list[tuple[int, int, str, str]] = []
            for line in f:
                parts = line.strip().split(":")
                if len(parts) < 4:
                    continue
                try:
                    uid = int(parts[2])
                    gid = int(parts[3])
                except ValueError:
                    continue
                if 1000 <= uid < 60000:
                    home = parts[5] if len(parts) > 5 else ""
                    entries.append((uid, gid, parts[0], home))
    except Exception:
        return None
    if not entries:
        return None
    for uid, gid, name, home in sorted(entries):
        if home and Path(home).exists():
            return uid, gid, name
    uid, gid, name, _ = entries[0]
    return uid, gid, name


def _owner_from_process_uid() -> tuple[int, int, str] | None:
    """Strategy 3 (last resort): use the running process's own UID,
    only if it's a 'human' UID (≥1000). Returns None for root/system."""
    uid = os.getuid()
    if uid < 1000:
        return None
    try:
        import pwd as _pwd2

        pw = _pwd2.getpwuid(uid)
        return uid, pw.pw_gid, pw.pw_name
    except Exception:
        return uid, os.getgid(), str(uid)


def detect_file_owner(paths: list[str] | None = None) -> tuple[int, int, str]:
    """Detect PUID/PGID from the owner of home directories or config paths.

    Returns (uid, gid, username). Falls back to first human user (UID≥1000)
    from /etc/passwd, then to the running process UID, then to 1000/1000.

    Step 2.7 phase-3 closure: 3 detection strategies extracted into
    `_owner_from_paths` / `_owner_from_etc_passwd` /
    `_owner_from_process_uid` — drops complexity from 15 to ≤ 4.
    """
    result = _owner_from_paths(paths or [])
    if result is not None:
        return result
    result = _owner_from_etc_passwd()
    if result is not None:
        return result
    result = _owner_from_process_uid()
    if result is not None:
        return result
    return 1000, 1000, ""


def detect_server_ip() -> str:
    """Get the primary LAN IP address (not loopback)."""
    import socket as _sock

    try:
        # Connect to an external address to determine the outbound interface
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return cast(str, ip)
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    try:
        return _sock.gethostbyname(_sock.gethostname())
    except Exception:
        return ""


def recommend_llm_with_gpu(
    headroom_mb: int, gpu: dict[str, Any], ram_bandwidth_gbs: float = 0.0
) -> tuple[str, list[str], str | None]:
    """Recommend LLM model: GPU VRAM first, then CPU RAM + bandwidth."""
    vram_mb = gpu.get("vram_mb", 0) if gpu.get("inference_capable") else 0

    if vram_mb >= 24_000:
        return (
            "llama3.3:70b-instruct-q4_K_M",
            ["llama3.3:70b-instruct-q4_K_M", "llama3.1:8b", "phi4-mini"],
            None,
        )
    if vram_mb >= 12_000:
        return ("llama3.1:8b", ["llama3.1:8b", "phi4-mini", "qwen2.5:3b"], None)
    if vram_mb >= 6_000:
        return ("phi4-mini", ["phi4-mini", "qwen2.5:3b", "llama3.2:3b", "smollm2:1.7b"], None)
    if vram_mb >= 3_000:
        return ("qwen2.5:3b", ["qwen2.5:3b", "llama3.2:3b", "smollm2:1.7b"], None)

    # CPU RAM path — pass bandwidth for speed-aware recommendation
    return recommend_llm(headroom_mb, ram_bandwidth_gbs=ram_bandwidth_gbs)


def benchmark_ram_bandwidth_gb() -> float:
    """Estimate RAM read bandwidth in GB/s using a simple array copy."""
    import time as _t
    import array as _arr

    size = 64 * 1024 * 1024  # 64 MB
    try:
        data = _arr.array("B", bytes(size))
        buf = _arr.array("B", bytes(size))
        start = _t.perf_counter()
        buf[:] = data
        elapsed = _t.perf_counter() - start
        return round((size / elapsed) / 1e9, 1)  # GB/s
    except Exception:
        return 0.0


def benchmark_cpu_gflops() -> float:
    """Very rough CPU floating-point throughput in GFLOPS."""
    import time as _t

    n = 2_000_000
    try:
        start = _t.perf_counter()
        x = 1.0
        for _ in range(n):
            x = x * 1.0000001 + 0.0000001
        elapsed = _t.perf_counter() - start
        flops = n * 2  # multiply + add
        return round(flops / elapsed / 1e9, 2)
    except Exception:
        return 0.0


def benchmark_storage_mb_s(path: str | None = None) -> float:
    """Sequential write speed in MB/s (16 MB test file)."""
    import time as _t
    import os as _os
    import tempfile as _tf

    if path is None:
        path = _tf.gettempdir()
    size = 16 * 1024 * 1024  # 16 MB
    data = bytes(size)
    try:
        fd, fpath = _tf.mkstemp(dir=path)
        try:
            start = _t.perf_counter()
            _os.write(fd, data)
            _os.fsync(fd)
            elapsed = _t.perf_counter() - start
            return round((size / elapsed) / 1e6, 0)
        finally:
            _os.close(fd)
            _os.unlink(fpath)
    except Exception:
        return 0.0


def _detect_gpu_rocm() -> dict[str, Any] | None:
    """Detect AMD discrete GPU via rocm-smi. Returns None on no-card,
    rocm-smi unavailable, or any parse failure."""
    try:
        import subprocess as _sp

        r = _sp.run(
            ["rocm-smi", "--showmeminfo", "vram", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        import json as _j

        data = _j.loads(r.stdout)
    except Exception:
        return None
    for card, info in data.items():
        vram_bytes = int(info.get("VRAM Total Memory (B)", 0))
        return {
            "vendor": "AMD",
            "name": card,
            "vram_mb": vram_bytes // (1024 * 1024),
            "inference_capable": True,
            "cuda_version": None,
            "backend": "rocm",
        }
    return None


def _amd_igpu_vram_mb() -> int:
    """Read AMD iGPU VRAM size from DRM. Returns -1 (shared memory
    sentinel) when no DRM file is present — APUs use system RAM,
    no fixed VRAM allocation."""
    try:
        import glob as _gl

        for vram_file in _gl.glob(
            "/sys/class/drm/card*/device/mem_info_vram_total",
        ):
            with open(vram_file) as _vf:
                return int(_vf.read().strip()) // (1024 * 1024)
    except Exception as e:
        log.debug("system probe skipped: %s", e)
    return -1


def _detect_gpu_amd_igpu() -> dict[str, Any] | None:
    """Detect AMD integrated GPU (Radeon Vega/RDNA APU) via `lspci -mm`.
    Returns None if lspci unavailable or no AMD VGA/display device found."""
    try:
        import subprocess as _sp

        r = _sp.run(["lspci", "-mm"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        lower = line.lower()
        is_gpu = "vga" in lower or "display" in lower or "3d" in lower
        is_amd = "amd" in lower or "ati" in lower or "radeon" in lower
        if not (is_gpu and is_amd):
            continue
        # lspci -mm format: slot "class" "vendor" "model" ...
        parts = [p.strip('"') for p in line.split('"')]
        gpu_name = parts[5] if len(parts) > 5 else "AMD Radeon (iGPU)"
        vram_mb = _amd_igpu_vram_mb()
        return {
            "vendor": "AMD",
            "name": gpu_name,
            "vram_mb": max(0, vram_mb),
            "vram_shared": vram_mb < 0,
            "inference_capable": False,  # iGPU not suitable for LLM inference
            "cuda_version": None,
            "backend": "amd_igpu",
        }
    return None


def _detect_gpu_apple_metal() -> dict[str, Any] | None:
    """Detect Apple Silicon GPU (Metal backend) via system_profiler.
    Only returns a result on Darwin/arm64 — otherwise None."""
    try:
        import subprocess as _sp

        r = _sp.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    import platform as _pl

    if _pl.system() != "Darwin" or _pl.machine() != "arm64":
        return None
    try:
        import json as _j

        data = _j.loads(r.stdout)
    except Exception:
        return None
    for disp in data.get("SPDisplaysDataType", []):
        return {
            "vendor": "Apple",
            "name": disp.get("sppci_model", "Apple Silicon"),
            "vram_mb": 0,  # shared — hard to query
            "inference_capable": True,
            "cuda_version": None,
            "backend": "metal",
        }
    return None


# Detection chain order matters — runs in declaration order, first hit wins.
# detect_gpu() handles NVIDIA (most common); the chain below covers ROCm
# (AMD discrete) → AMD iGPU → Apple Metal.
_GPU_EXTENDED_DETECTORS: tuple[Callable[[], dict[str, Any] | None], ...] = (
    _detect_gpu_rocm,
    _detect_gpu_amd_igpu,
    _detect_gpu_apple_metal,
)


def detect_gpu_extended() -> dict[str, Any]:
    """Extend detect_gpu() with ROCm (AMD) and Apple Metal detection.

    Step 2.7.f: extracts each detection vendor into its own helper
    (`_detect_gpu_rocm`, `_detect_gpu_amd_igpu`, `_detect_gpu_apple_metal`)
    and walks them via a tuple-table chain — drops complexity from
    17 to ≤ 4.
    """
    base = detect_gpu()
    if base.get("name"):
        return base
    for detector in _GPU_EXTENDED_DETECTORS:
        result = detector()
        if result is not None:
            return result
    return base


# ---------------------------------------------------------------------------
# Profile cache — 60 second TTL so Stage 0 revisits don't re-run subprocesses
# ---------------------------------------------------------------------------

_profile_cache: SystemProfile | None = None
_profile_cache_at: float = 0.0
_PROFILE_CACHE_TTL: float = 60.0  # seconds


def get_cached_profile(
    selected_app_keys: list[str] | None = None,
    config_root: str = "/",
    media_root: str = "/",
    force: bool = False,
) -> SystemProfile:
    """Return a cached SystemProfile if fresh, otherwise re-evaluate.

    The cache is intentionally process-level (module global) — the prereqs
    endpoint is called once per wizard stage visit, sometimes in rapid
    succession. A 60-second TTL means the profile stays valid across a full
    wizard session while still refreshing between sessions.
    """
    global _profile_cache, _profile_cache_at
    now = time.time()
    if not force and _profile_cache is not None and (now - _profile_cache_at) < _PROFILE_CACHE_TTL:
        log.debug("system_eval: returning cached profile (%.0fs old)", now - _profile_cache_at)
        return _profile_cache
    profile = evaluate_system(selected_app_keys, config_root, media_root)
    _profile_cache = profile
    _profile_cache_at = now
    return profile


def invalidate_profile_cache() -> None:
    """Force re-evaluation on the next prereqs call (call after wizard completes)."""
    global _profile_cache, _profile_cache_at
    _profile_cache = None
    _profile_cache_at = 0.0


def evaluate_model_compatibility(
    model_size_gb: float,
    quantization: str,
    system_ram_gb: float,
    available_ram_gb: float,
    cpu_cores: int,
    avx2: bool,
    gpu: dict[str, Any],
    storage_free_gb: float,
) -> dict[str, Any]:
    """Full hardware compatibility check for a GGUF model."""
    issues, warnings, recommendations = [], [], []
    ram_needed = model_size_gb * 1.15
    if available_ram_gb < model_size_gb:
        issues.append(
            f"Insufficient RAM: need ~{ram_needed:.1f} GB, have {available_ram_gb:.1f} GB"
        )
    elif available_ram_gb < ram_needed:
        warnings.append(
            f"RAM tight: {available_ram_gb:.1f} GB available, need ~{ram_needed:.1f} GB"
        )
    if not avx2:
        issues.append("CPU lacks AVX2 — required by llama.cpp for CPU inference")
    if storage_free_gb < model_size_gb:
        issues.append(
            f"Insufficient storage: need {model_size_gb:.1f} GB, have {storage_free_gb:.1f} GB"
        )
    gpu_vram_gb = gpu.get("vram_mb", 0) / 1024
    if gpu.get("inference_capable") and gpu_vram_gb >= model_size_gb:
        recommendations.append(f"GPU ({gpu['name']}) can run this model in VRAM")
        mode, tps = "gpu", min(80, max(20, int(gpu_vram_gb / model_size_gb * 30)))
    else:
        mode, tps = "cpu", max(1, int((cpu_cores / 4) * max(1.0, 5.0 - model_size_gb * 0.5)))
    if issues:
        verdict, summary = "cannot_run", f"Cannot run: {issues[0]}"
    elif warnings:
        verdict, summary = "runs_slowly", f"Runs with caveats (~{tps} tok/s, {mode})"
    else:
        verdict, summary = "runs_well", f"Compatible ✓ (~{tps} tok/s, {mode})"
    return {
        "verdict": verdict,
        "summary": summary,
        "issues": issues,
        "warnings": warnings,
        "recommendations": recommendations,
        "estimated_tokens_per_second": tps,
        "inference_mode": mode,
    }


def evaluate_system(
    selected_app_keys: list[str] | None = None,
    config_root: str = "/",
    media_root: str = "/",
) -> SystemProfile:
    """Run a full system evaluation.

    Returns a SystemProfile with hardware measurements, stack projection,
    and LLM model recommendation.
    """
    # ── Fast synchronous reads (file I/O only, ~1ms total) ──────────────
    mem = read_meminfo()
    total_ram_kb = mem.get("MemTotal", 0)
    available_ram_kb = mem.get("MemAvailable", 0)
    total_ram_mb = total_ram_kb // 1024
    free_ram_mb = available_ram_kb // 1024
    used_ram_mb = total_ram_mb - free_ram_mb

    # Single /proc/cpuinfo read — extracts cores, model, AND avx flags
    cpu_cores, cpu_model, avx_info = read_cpuinfo()
    arch = platform.machine()
    os_info = detect_os()

    # ── Slow I/O: subprocess calls run in parallel ────────────────────────
    # On a live server: docker stats ~3-8s, GPU detect ~0.5-2s, docker version ~0.5s
    # Running concurrently brings total to max(longest_task) instead of sum(all_tasks)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _hint_paths = [p for p in [config_root, media_root, "/home"] if p != "/"]
    slow_tasks = {
        "gpu": detect_gpu_extended,
        "docker_ver": detect_docker_version,
        "docker_ram": docker_ram_usage_mb,
        "containers": detect_running_containers,
        "timezone": detect_timezone,
        "file_owner": lambda: detect_file_owner(_hint_paths),
        "server_ip": detect_server_ip,
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(slow_tasks), thread_name_prefix="prereqs") as ex:
        futures = {ex.submit(fn): name for name, fn in slow_tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as _e:
                log.debug("prereqs task %s failed: %s", name, _e)
                results[name] = None

    gpu_info = results.get("gpu") or {}
    docker_info = results.get("docker_ver") or {"engine": "", "api": "", "compose": ""}
    container_ram_mb = results.get("docker_ram") or 0
    containers_running = results.get("containers") or 0
    tz = results.get("timezone") or "UTC"
    _fo = results.get("file_owner") or (1000, 1000, "")
    puid, pgid, puid_username = _fo if isinstance(_fo, tuple) else (1000, 1000, "")
    server_ip = results.get("server_ip") or ""

    # ── Disks (statvfs — fast, no subprocess) ────────────────────────────
    disks = []
    for path in dict.fromkeys(["/", config_root, media_root]):  # deduplicated, ordered
        info = disk_usage(path)
        if info:
            disks.append(info)

    # Stack projection
    apps = selected_app_keys or []
    estimated_mb = estimate_stack_ram(apps)
    safety_buffer = int(total_ram_mb * SAFETY_BUFFER_FRACTION)
    headroom_mb = total_ram_mb - estimated_mb - safety_buffer

    # LLM recommendation (GPU-aware)
    # Run RAM bandwidth benchmark once (fast, ~50ms)
    try:
        ram_bw_gbs = benchmark_ram_bandwidth_gb()
    except Exception:
        ram_bw_gbs = 0.0

    rec_model, avail_models, llm_warning = recommend_llm_with_gpu(
        max(0, headroom_mb), gpu_info, ram_bandwidth_gbs=ram_bw_gbs
    )

    return SystemProfile(
        cpu_cores=cpu_cores,
        avx=avx_info["avx"],
        avx2=avx_info["avx2"],
        avx512=avx_info["avx512"],
        gpu_vendor=gpu_info.get("vendor"),
        gpu_name=gpu_info.get("name"),
        gpu_vram_mb=gpu_info.get("vram_mb", 0),
        gpu_inference_capable=gpu_info.get("inference_capable", False),
        gpu_cuda_version=gpu_info.get("cuda_version"),
        cpu_model=cpu_model,
        total_ram_mb=total_ram_mb,
        architecture=arch,
        used_ram_mb=used_ram_mb,
        free_ram_mb=free_ram_mb,
        docker_container_ram_mb=container_ram_mb,
        disks=disks,
        selected_apps=apps,
        estimated_stack_ram_mb=estimated_mb,
        headroom_ram_mb=max(0, headroom_mb),
        recommended_model=rec_model,
        available_models=avail_models,
        llm_warning=llm_warning,
        os_distro=os_info["distro"],
        os_version=os_info["version"],
        os_arch=os_info["arch"],
        kernel_version=os_info["kernel"],
        docker_version=docker_info["engine"],
        docker_api_version=docker_info["api"],
        compose_version=docker_info["compose"],
        containers_running=containers_running,
        puid=puid,
        pgid=pgid,
        puid_username=puid_username,
        timezone=tz,
        server_ip=server_ip,
        measured_at=int(time.time()),
    )


def quick_ram_check(model_ram_mb: int) -> tuple[bool, str | None]:
    """Fast check: can this model run right now without disrupting the stack?

    Returns (ok, warning_or_none). Called before each LLM inference.
    """
    mem = read_meminfo()
    available_mb = mem.get("MemAvailable", 0) // 1024
    needed_mb = model_ram_mb + 512  # 512MB buffer for inference overhead

    if available_mb < needed_mb:
        return False, (
            f"Only {available_mb}MB RAM available, need {needed_mb}MB for this model. "
            f"Skipping LLM — using rule-based healing only."
        )
    return True, None
