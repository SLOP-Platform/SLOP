"""backend/core/gguf_validator.py

Validates GGUF model files and manages downloads from HuggingFace or direct URLs.

GGUF format reference: https://github.com/ggerganov/ggml/blob/master/docs/gguf.md

Magic bytes: b'GGUF' (0x47 0x47 0x55 0x46)
Version: uint32 LE (1, 2, or 3)
tensor_count: uint64 LE (v2+)
metadata_kv_count: uint64 LE (v2+)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Generator

from backend.core.logging import get_logger

log = get_logger(__name__)

GGUF_MAGIC = b"GGUF"
GGUF_SUPPORTED_VERSIONS = {1, 2, 3}
MIN_FILE_SIZE_MB = 100  # Anything < 100MB is almost certainly not a usable model
MAX_REASONABLE_SIZE_GB = 8  # Warn (not block) if > 8GB


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class GGUFValidationResult:
    valid: bool
    path: Path
    file_size_mb: float
    gguf_version: int | None
    error: str | None = None  # plain-language error if not valid
    warning: str | None = None  # non-blocking advisory


@dataclass
class DownloadProgress:
    url: str
    bytes_downloaded: int
    total_bytes: int | None  # None if server doesn't send Content-Length
    percent: float | None
    done: bool = False
    error: str | None = None

    @property
    def mb_downloaded(self) -> float:
        return self.bytes_downloaded / 1_048_576


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_gguf(path: Path) -> GGUFValidationResult:
    """Validate that a file is a well-formed GGUF model.

    Checks:
      1. File exists and is readable
      2. Minimum size (not a stub/placeholder)
      3. GGUF magic bytes at offset 0
      4. Supported version number
      5. Non-zero tensor count (v2+)

    Returns a GGUFValidationResult — never raises.
    """
    if not path.exists():
        return GGUFValidationResult(
            valid=False,
            path=path,
            file_size_mb=0,
            gguf_version=None,
            error=f"File not found: {path}",
        )

    if not path.is_file():
        return GGUFValidationResult(
            valid=False,
            path=path,
            file_size_mb=0,
            gguf_version=None,
            error=f"Path is not a file: {path}",
        )

    size_bytes = path.stat().st_size
    size_mb = size_bytes / 1_048_576

    if size_bytes < MIN_FILE_SIZE_MB * 1_048_576:
        return GGUFValidationResult(
            valid=False,
            path=path,
            file_size_mb=size_mb,
            gguf_version=None,
            error=(
                f"File is too small ({size_mb:.1f} MB). "
                f"A usable GGUF model is at least {MIN_FILE_SIZE_MB} MB. "
                f"This may be an incomplete download or a placeholder file."
            ),
        )

    try:
        with open(path, "rb") as f:
            # Read magic (4 bytes)
            magic = f.read(4)
            if magic != GGUF_MAGIC:
                readable = magic.decode("ascii", errors="replace")
                return GGUFValidationResult(
                    valid=False,
                    path=path,
                    file_size_mb=size_mb,
                    gguf_version=None,
                    error=(
                        f"Not a GGUF file — magic bytes are '{readable}', expected 'GGUF'. "
                        f"Make sure you downloaded a .gguf file, not a .safetensors or .bin file."
                    ),
                )

            # Read version (uint32 LE)
            version_bytes = f.read(4)
            if len(version_bytes) < 4:
                return GGUFValidationResult(
                    valid=False,
                    path=path,
                    file_size_mb=size_mb,
                    gguf_version=None,
                    error="File is truncated — could not read GGUF version. Re-download the file.",
                )

            version = struct.unpack("<I", version_bytes)[0]

            if version not in GGUF_SUPPORTED_VERSIONS:
                return GGUFValidationResult(
                    valid=False,
                    path=path,
                    file_size_mb=size_mb,
                    gguf_version=version,
                    error=(
                        f"Unsupported GGUF version {version}. "
                        f"Supported versions: {sorted(GGUF_SUPPORTED_VERSIONS)}. "
                        f"Try a newer llama.cpp server image."
                    ),
                )

            # For v2+, read tensor_count (uint64 LE) — must be > 0
            tensor_count: int | None = None
            if version >= 2:
                tc_bytes = f.read(8)
                if len(tc_bytes) == 8:
                    tensor_count = struct.unpack("<Q", tc_bytes)[0]
                    if tensor_count == 0:
                        return GGUFValidationResult(
                            valid=False,
                            path=path,
                            file_size_mb=size_mb,
                            gguf_version=version,
                            error=(
                                "GGUF file reports zero tensors. "
                                "The file may be corrupt or from an unsupported export tool."
                            ),
                        )

    except OSError as e:
        return GGUFValidationResult(
            valid=False,
            path=path,
            file_size_mb=size_mb,
            gguf_version=None,
            error=f"Could not read file: {e}",
        )

    warning: str | None = None
    if size_mb > MAX_REASONABLE_SIZE_GB * 1024:
        warning = (
            f"This model is {size_mb / 1024:.1f} GB. "
            f"Models over {MAX_REASONABLE_SIZE_GB} GB may be slow on CPU-only systems."
        )

    log.info(
        "GGUF validated: %s — %.1f MB, version %d, tensors=%s",
        path.name,
        size_mb,
        version,
        tensor_count,
    )

    return GGUFValidationResult(
        valid=True,
        path=path,
        file_size_mb=size_mb,
        gguf_version=version,
        warning=warning,
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def resolve_gguf_url(url_or_hf: str) -> str:
    """Resolve a HuggingFace shorthand or plain URL to a downloadable URL.

    Supported formats:
      hf://org/repo/filename.gguf
        → https://huggingface.co/org/repo/resolve/main/filename.gguf
      https://huggingface.co/org/repo/blob/main/filename.gguf
        → https://huggingface.co/org/repo/resolve/main/filename.gguf  (blob→resolve)
      https://any-direct-url/model.gguf
        → unchanged

    HuggingFace /blob/ URLs return an HTML page — must be converted to /resolve/.
    """
    if url_or_hf.startswith("hf://"):
        # hf://org/repo/path/to/file.gguf
        rest = url_or_hf[5:]
        parts = rest.split("/", 2)
        if len(parts) < 3:
            raise ValueError(
                f"Invalid HuggingFace shorthand '{url_or_hf}'. "
                f"Expected format: hf://org/repo/filename.gguf"
            )
        org, repo, filepath = parts
        return f"https://huggingface.co/{org}/{repo}/resolve/main/{filepath}"

    # Fix HuggingFace /blob/ URLs
    if "huggingface.co" in url_or_hf and "/blob/" in url_or_hf:
        url_or_hf = url_or_hf.replace("/blob/", "/resolve/")

    # Enforce https-only scheme before any network call.  file://, http://,
    # ftp://, and custom schemes are rejected here — they can read local files
    # or bypass TLS.  The S310 noqa on the urlopen call site is justified by
    # this check: by the time urlopen is reached, the scheme is guaranteed https.
    if not url_or_hf.startswith("https://"):
        raise ValueError(
            f"Unsupported URL scheme in {url_or_hf!r}. "
            f"Only https:// URLs are accepted (use hf:// for HuggingFace)."
        )

    return url_or_hf


def _assert_safe_url(url: str) -> None:
    """Raise ValueError if url is not https. Keeps urlopen call sites branch-free."""
    if not url.startswith("https://"):
        raise ValueError(f"Refusing non-https URL: {url!r}")


def download_gguf(
    url_or_hf: str,
    dest_dir: Path,
    filename: str | None = None,
    hf_token: str = "",
) -> Generator[DownloadProgress, None, GGUFValidationResult]:
    """Stream-download a GGUF file with progress, then validate it.

    This is a generator — yield DownloadProgress objects during download,
    then return a GGUFValidationResult when done (via StopIteration.value).

    Usage:
        gen = download_gguf("hf://org/repo/model.gguf", models_dir)
        for progress in gen:
            print(f"{progress.percent:.1f}%")
        result = gen.value  # only works in Python 3.7+ with explicit send/throw

    Or simpler — collect all yields:
        result = yield from download_gguf(...)
    """
    import urllib.request
    import urllib.error

    try:
        url = resolve_gguf_url(url_or_hf)
    except ValueError as e:
        yield DownloadProgress(
            url=url_or_hf,
            bytes_downloaded=0,
            total_bytes=None,
            percent=None,
            done=True,
            error=str(e),
        )
        return GGUFValidationResult(
            valid=False, path=Path(), file_size_mb=0, gguf_version=None, error=str(e)
        )

    # Determine filename
    if not filename:
        filename = url.split("/")[-1].split("?")[0]
        if not filename.endswith(".gguf"):
            filename += ".gguf"

    dest_path = dest_dir / filename
    dest_dir.mkdir(parents=True, exist_ok=True)

    _assert_safe_url(url)  # local invariant: scheme is https before urlopen
    try:
        headers = {"User-Agent": "SLOP/3.0"}
        if hf_token and "huggingface.co" in url:
            headers["Authorization"] = f"Bearer {hf_token}"
        req = urllib.request.Request(url, headers=headers)  # noqa: S310  # nosec B310  # https enforced by resolve_gguf_url + _assert_safe_url
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310  # nosec B310  # https enforced by resolve_gguf_url + _assert_safe_url
            total = int(resp.headers.get("Content-Length", 0)) or None
            downloaded = 0
            chunk_size = 1_048_576  # 1 MB chunks

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    pct = (downloaded / total * 100) if total else None
                    yield DownloadProgress(
                        url=url,
                        bytes_downloaded=downloaded,
                        total_bytes=total,
                        percent=pct,
                    )

    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code} downloading from {url}. " + (
            "Check the URL is correct and the file is publicly accessible."
            if e.code == 404
            else f"Server error: {e.reason}"
        )
        if dest_path.exists():
            dest_path.unlink()
        yield DownloadProgress(
            url=url, bytes_downloaded=0, total_bytes=None, percent=None, done=True, error=msg
        )
        return GGUFValidationResult(
            valid=False, path=dest_path, file_size_mb=0, gguf_version=None, error=msg
        )
    except OSError as e:
        msg = f"Download failed: {e}"
        if dest_path.exists():
            dest_path.unlink()
        yield DownloadProgress(
            url=url, bytes_downloaded=0, total_bytes=None, percent=None, done=True, error=msg
        )
        return GGUFValidationResult(
            valid=False, path=dest_path, file_size_mb=0, gguf_version=None, error=msg
        )

    # Download complete — validate
    result = validate_gguf(dest_path)
    if not result.valid and dest_path.exists():
        # Remove corrupt download
        dest_path.unlink()
        log.warning("Removed invalid GGUF download: %s — %s", dest_path, result.error)

    yield DownloadProgress(
        url=url,
        bytes_downloaded=downloaded,
        total_bytes=total,
        percent=100.0,
        done=True,
        error=result.error if not result.valid else None,
    )
    return result


# ---------------------------------------------------------------------------
# Model directory helpers
# ---------------------------------------------------------------------------


def list_gguf_files(models_dir: Path) -> list[dict[str, Any]]:
    """List all GGUF files in a directory with their metadata."""
    if not models_dir.exists():
        return []

    results = []
    for path in sorted(models_dir.glob("*.gguf")):
        vr = validate_gguf(path)
        results.append(
            {
                "filename": path.name,
                "path": str(path),
                "size_mb": round(vr.file_size_mb, 1),
                "valid": vr.valid,
                "gguf_version": vr.gguf_version,
                "error": vr.error,
                "warning": vr.warning,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Recommended models catalogue (for the UI download helper)
# ---------------------------------------------------------------------------


RECOMMENDED_MODELS = [
    {
        "name": "Phi-4 Mini",
        "hf_url": "hf://bartowski/microsoft_Phi-4-mini-instruct-GGUF/microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
        "size_gb": 2.4,
        "recommended_for": "slop-agent",
        "notes": "Best diagnostic reasoning. Default health agent model.",
    },
    {
        "name": "Llama 3.2 3B",
        "hf_url": "hf://bartowski/Llama-3.2-3B-Instruct-GGUF/Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "size_gb": 2.0,
        "recommended_for": "general",
        "notes": "Solid general-purpose fallback.",
    },
    {
        "name": "Qwen 2.5 3B",
        "hf_url": "hf://Qwen/Qwen2.5-3B-Instruct-GGUF/qwen2.5-3b-instruct-q4_k_m.gguf",
        "size_gb": 2.0,
        "recommended_for": "json",
        "notes": "Best for structured JSON output and strict parsing.",
    },
    {
        "name": "Gemma 3 4B",
        "hf_url": "hf://bartowski/gemma-3-4b-it-GGUF/gemma-3-4b-it-Q4_K_M.gguf",
        "size_gb": 2.5,
        "recommended_for": "reasoning",
        "notes": "Strong reasoning, slightly larger footprint.",
    },
    {
        "name": "SmolLM2 1.7B",
        "hf_url": "hf://HuggingFaceTB/SmolLM2-1.7B-Instruct-GGUF/smollm2-1.7b-instruct-q4_k_m.gguf",
        "size_gb": 1.0,
        "recommended_for": "fast",
        "notes": "Fastest inference. Best for simple triage and classification.",
    },
]
