#!/usr/bin/env bash
# install.sh — v5 bootstrap: parse flags, TTY detect, distro check,
#              install python3 + git, then exec to installer/main.py.
#
# curl|bash (headline path):
#   curl -fsSL https://raw.githubusercontent.com/SLOP-Platform/SLOP/main/install.sh \
#     | sudo bash -s -- --install-docker=yes
#
# git-clone (inspectable path):
#   git clone https://github.com/SLOP-Platform/SLOP.git
#   cd SLOP && sudo ./install.sh

set -euo pipefail

# Canonical default-value definitions (INV-1 allowlist; install.sh is not
# in installer/ so Rule 5.26 does not scan it, but the comment makes the
# intent auditable).
_DEFAULT_INSTALL_DIR="/opt/slop"
_DEFAULT_DATA_DIR="/var/lib/slop"

# ── Step 1: Parse arguments into environment variables ───────────────────────
MS_INSTALL_DIR="${MS_INSTALL_DIR:-${_DEFAULT_INSTALL_DIR}}"
MS_DATA_DIR="${MS_DATA_DIR:-${_DEFAULT_DATA_DIR}}"
MS_INSTALL_DOCKER=""
MS_VERSION_REF=""
MS_FORCE=0
MS_VERIFY=0
MS_SKIP_TREE_VERIFY=0

while [ $# -gt 0 ]; do
  case "$1" in
  --install-dir=*) MS_INSTALL_DIR="${1#*=}" ;;
  --install-dir)
    MS_INSTALL_DIR="$2"
    shift
    ;;
  --data-dir=*) MS_DATA_DIR="${1#*=}" ;;
  --data-dir)
    MS_DATA_DIR="$2"
    shift
    ;;
  --install-docker=*) MS_INSTALL_DOCKER="${1#*=}" ;;
  --install-docker)
    MS_INSTALL_DOCKER="$2"
    shift
    ;;
  --version-ref=*) MS_VERSION_REF="${1#*=}" ;;
  --version-ref)
    MS_VERSION_REF="$2"
    shift
    ;;
  --force) MS_FORCE=1 ;;
  --verify) MS_VERIFY=1 ;;
  --skip-tree-verify) MS_SKIP_TREE_VERIFY=1 ;;
  *) ;; 
  esac
  shift
done
export MS_INSTALL_DIR MS_DATA_DIR MS_INSTALL_DOCKER MS_VERSION_REF MS_FORCE MS_VERIFY MS_SKIP_TREE_VERIFY

# ── Step 2: Verify installer integrity (--verify) ────────────────────────────
#
# --verify checksums THIS bootstrap script (install.sh) against the
# release-published install.sh.sha256.
#
# Tree integrity (the installer/backend tree cloned and run as root) is
# verified separately by the Python installer: after cloning a tagged release,
# fetch_repo() downloads a per-release tree.checksums manifest and runs
# sha256sum -c against the cloned tree.  Verification is automatic for v5
# tagged releases; use --skip-tree-verify to disable.
if [ "$MS_VERIFY" -eq 1 ]; then
  _ref="${MS_VERSION_REF:-main}"
  _checksum_url="https://github.com/SLOP-Platform/SLOP/releases/download/${_ref}/install.sh.sha256"
  echo "Verifying installer integrity against ${_checksum_url} ..."
  echo "NOTE: --verify covers THIS bootstrap script (install.sh) only." >&2
  echo "      The cloned installer/backend tree is verified separately via a" >&2
  echo "      per-release checksum manifest (automatic for tagged releases)." >&2

  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    echo "ERROR: --verify requires curl or wget to download the checksum." >&2
    exit 1
  fi

  if ! command -v sha256sum >/dev/null 2>&1; then
    echo "ERROR: --verify requires sha256sum (coreutils)." >&2
    exit 1
  fi

  _tmp_checksum="$(mktemp)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$_checksum_url" -o "$_tmp_checksum" 2>/dev/null
  else
    wget -qO "$_tmp_checksum" "$_checksum_url" 2>/dev/null
  fi

  if [ ! -s "$_tmp_checksum" ]; then
    rm -f "$_tmp_checksum"
    echo "ERROR: Could not download checksum from ${_checksum_url}" >&2
    echo "  If you are running from a development checkout, specify --version-ref=<tag>." >&2
    exit 1
  fi

  # The downloaded file contains "<hash>  install.sh" or "<hash>  -".
  # Extract just the hash.
  _expected_hash="$(awk '{print $1}' "$_tmp_checksum")"
  rm -f "$_tmp_checksum"

  _actual_hash="$(sha256sum "$0" | awk '{print $1}')"

  if [ "$_actual_hash" = "$_expected_hash" ]; then
    echo "OK: bootstrap installer script (install.sh) checksum verified (${_actual_hash})."
    echo "    The cloned tree will be verified separately via per-release checksum manifest."
  else
    echo "ERROR: checksum mismatch." >&2
    echo "  Expected: ${_expected_hash}" >&2
    echo "  Actual:   ${_actual_hash}" >&2
    echo "  The installer file may have been modified in transit." >&2
    exit 1
  fi
fi

# ── Step 4: Detect whether stdin is a TTY ────────────────────────────────────
if [ -t 0 ]; then
  _TTY_MODE="interactive"
else
  _TTY_MODE="pipe"
fi

# ── Step 5: Pipe-mode fail-fast — before any filesystem write or apt call ────
#
# ADR 0013 §3: "The check is the first thing install.sh does after parsing
# arguments and detecting pipe mode — before detect_os(), before
# check_prereqs(), before any state-file write, before any apt install."
if [ "$_TTY_MODE" = "pipe" ] && [ -z "$MS_INSTALL_DOCKER" ]; then
  echo "ERROR: Running in pipe mode (curl|bash) without --install-docker flag." >&2
  echo "" >&2
  echo "  Pipe mode requires an explicit Docker decision:" >&2
  echo "" >&2
  echo "    curl -fsSL <URL> | sudo bash -s -- --install-docker=yes" >&2
  echo "    curl -fsSL <URL> | sudo bash -s -- --install-docker=no" >&2
  echo "" >&2
  echo "  --install-docker=yes  Install Docker via get.docker.com if absent." >&2
  echo "  --install-docker=no   Skip Docker install; ensure Docker is already present." >&2
  echo "" >&2
  echo "No files have been written. Re-run with the flag to proceed." >&2
  exit 1
fi

# Root check (after fail-fast; before any mutating step).
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: install.sh must be run as root (use: sudo ./install.sh)." >&2
  exit 1
fi

# ── Step 6: Detect distro (read-only) ────────────────────────────────────────
if [ ! -f /etc/os-release ]; then
  echo "ERROR: /etc/os-release not found; cannot detect OS." >&2
  exit 1
fi

# shellcheck source=/dev/null
. /etc/os-release

_distro_ok=0
case "${ID:-}" in
debian)
  _maj="${VERSION_ID%%.*}"
  if [ "$_maj" -ge 12 ] 2>/dev/null && [ "$_maj" -le 13 ] 2>/dev/null; then _distro_ok=1; fi
  ;;
ubuntu)
  _vmaj="${VERSION_ID%%.*}"
  _vmin="${VERSION_ID#*.}"
  if [ "$_vmaj" -eq 24 ] 2>/dev/null && [ "$_vmin" -ge 4 ] 2>/dev/null; then
    _distro_ok=1
  fi
  ;;
esac

if [ "$_distro_ok" -eq 0 ]; then
  echo "ERROR: Unsupported distribution: ${PRETTY_NAME:-${ID:-unknown} ${VERSION_ID:-}}." >&2
  echo "" >&2
  echo "  Supported distributions (v5.0.0):" >&2
  echo "    Ubuntu 24.04 LTS (Noble Numbat)" >&2
  echo "    Debian 12 (Bookworm)" >&2
  echo "    Debian 13 (Trixie)" >&2
  echo "" >&2
  echo "  See installer/SUPPORTED_DISTROS.md for the full support matrix." >&2
  echo "  See docs/adr/0016-supported-distro-set.md for policy rationale." >&2
  exit 1
fi

# ── Step 7: Bootstrap python3 (3.11+), python3-venv, and git ─────────────────
_needs_apt_update=0
_MS_PYTHON3=python3

if ! command -v git >/dev/null 2>&1; then
  _needs_apt_update=1
fi

_py3_ok=0
if command -v python3 >/dev/null 2>&1; then
  _pyver=$(python3 -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)" 2>/dev/null || echo 0)
  if [ "$_pyver" -ge 311 ]; then _py3_ok=1; fi
fi
if [ "$_py3_ok" -eq 0 ]; then
  _needs_apt_update=1
fi

# python3-venv check — independent of python3 version check (F1 fix).
# python3 presence does NOT imply python3-venv on Debian/Ubuntu; it is a
# separate sub-package providing ensurepip support used at venv-creation time.
# Probe by attempting real venv creation — the same operation backend.py will
# invoke.  --without-pip bypasses ensurepip and would give a false-positive.
_pyvenv_ok=0
if [ "$_py3_ok" -eq 1 ]; then
  if python3 -m venv /tmp/_ms_venv_probe >/dev/null 2>&1; then
    _pyvenv_ok=1
  fi
  rm -rf /tmp/_ms_venv_probe 2>/dev/null || true
fi
if [ "$_pyvenv_ok" -eq 0 ]; then
  _needs_apt_update=1
fi

if [ "$_needs_apt_update" -eq 1 ]; then
  apt-get update -qq
fi

if ! command -v git >/dev/null 2>&1; then
  apt-get install -y --no-install-recommends git -qq
fi

if [ "$_py3_ok" -eq 0 ]; then
  case "${ID:-}" in
  ubuntu | debian)
    # Ubuntu 24.04 ships Python 3.12; Debian 12 ships 3.11; Debian 13 ships 3.13.
    # All supported distros provide python3 >= 3.11 in main — no PPA required.
    apt-get install -y --no-install-recommends python3 python3-venv -qq
    ;;
  esac
elif [ "$_pyvenv_ok" -eq 0 ]; then
  # python3 >= 3.11 is present but python3-venv is missing (F1 fix path).
  # Install python3-venv independently — no python3 version-selection needed.
  case "${ID:-}" in
  ubuntu | debian)
    apt-get install -y --no-install-recommends python3-venv -qq
    ;;
  esac
fi

# ── Locate repo root ──────────────────────────────────────────────────────────
# Git-clone path: install.sh lives at the repo root; $0's directory is the repo.
# curl|bash path: script is not inside a git repo; clone to a temp dir.
_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if git -C "$_SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  _REPO_DIR="$_SCRIPT_DIR"
else
  _REPO_DIR="$(mktemp -d)"
  git clone --branch "${MS_VERSION_REF:-main}" "https://github.com/SLOP-Platform/SLOP.git" "$_REPO_DIR" --quiet
fi

# ── Hand off to the Python installer ─────────────────────────────────────────
# Pass resolved values as explicit flags so main.py receives a complete
# Namespace regardless of whether the user omitted path flags on the CLI.
_PY_ARGS=(install
  --install-dir="$MS_INSTALL_DIR"
  --data-dir="$MS_DATA_DIR")
[ -n "$MS_INSTALL_DOCKER" ] && _PY_ARGS+=(--install-docker="$MS_INSTALL_DOCKER")
[ -n "$MS_VERSION_REF" ] && _PY_ARGS+=(--version-ref="$MS_VERSION_REF")
[ "$MS_FORCE" -eq 1 ] && _PY_ARGS+=(--force)
[ "$MS_SKIP_TREE_VERIFY" -eq 1 ] && _PY_ARGS+=(--skip-tree-verify)
cd "$_REPO_DIR" && exec "$_MS_PYTHON3" -m installer.main "${_PY_ARGS[@]}"
