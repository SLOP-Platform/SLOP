#!/usr/bin/env bash
# deploy.sh — Deploy SLOP v3 to an Ubuntu 24.04 server
#
# Usage:
#   ./deploy.sh                    # Full install (first time)
#   ./deploy.sh --update           # Pull latest and restart (use ms-update instead)
#   ./deploy.sh --frontend-only    # Rebuild frontend and restart
#   ./deploy.sh --help             # Show this usage and exit
#
# What this does:
#   1. Checks system dependencies (Python, Node, Docker, rsync)
#   2. Creates required directories
#   3. Installs Python packages into a virtualenv
#   4. Builds the Vue 3 frontend
#   5. Creates .env from template (if missing)
#   6. Installs and enables the slop systemd service
#   7. Installs ms-update as a system command
#   8. Runs a smoke test

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────
# Single install location: the repo IS the install dir.
# No more copying to /opt — edits and updates happen in one place.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$SCRIPT_DIR"

# ── Argument parsing (early — before sourcing the helper) ──────────────────
# --help is parsed first so it works even before deploy_lib.sh exists.
UPDATE_ONLY=false
FRONTEND_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --update)        UPDATE_ONLY=true ;;
    --frontend-only) FRONTEND_ONLY=true ;;
    --help|-h)
      echo "Usage: $0 [--update] [--frontend-only] [--help]"
      echo ""
      echo "  (no flags)        Full install — create venv, build frontend, install systemd unit"
      echo "  --update          Fetch + reset to origin/main, rebuild, restart (same as ms-update)"
      echo "  --frontend-only   Rebuild Vue frontend and restart the service"
      echo "  --help            Show this message and exit"
      exit 0 ;;
    *)
      echo "  ! Unknown flag: $arg" >&2 ;;
  esac
done

# ── Shared helper ──────────────────────────────────────────────────────────
# Provides: detect_service_user, build_home, normalize_ownership
# NOTE: tools/deploy_lib.sh must exist before this script runs; bash -n
# still passes because source is valid syntax even when the file is absent.
# shellcheck source=tools/deploy_lib.sh
source "$INSTALL_DIR/tools/deploy_lib.sh"

# ── Service user (resolved from install-dir owner, not the login user) ─────
# detect_service_user: stat -c %U <dir> → systemctl show slop -p User
#   → literal "slop"  (PINNED contract from deploy_lib.sh)
SERVICE_USER="$(detect_service_user "$INSTALL_DIR")"

VENV_DIR="$INSTALL_DIR/.venv"
DATA_DIR="${MS_DATA_DIR:-$INSTALL_DIR/data}"

# ── Canonical service port (PINNED — B owns this name; A + D consume it) ───
# MS_PORT is the canonical var written to .env and baked into the unit.
BIND_PORT="${MS_PORT:-8080}"

API_URL="http://localhost:${BIND_PORT}"
SERVICE_FILE="/etc/systemd/system/slop.service"

# ── Colors ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "  ${CYAN}→${RESET} $*"; }
ok()    { echo -e "  ${GREEN}✓${RESET} $*"; }
warn()  { echo -e "  ${YELLOW}!${RESET} $*"; }
err()   { echo -e "  ${RED}✗${RESET} $*" >&2; }
step()  { echo -e "\n${BOLD}$*${RESET}"; }
die()   { err "$*"; exit 1; }
SUDO="sudo"
# Note: sudo is always used for privileged ops (systemctl, tee to /etc, ln to /usr/local/bin).
# File-touching git/pip/npm operations run as SERVICE_USER via "sudo -u $SERVICE_USER".

echo
echo -e "${BOLD}  SLOP v3 — Deploy${RESET}"
echo -e "  Install dir: ${CYAN}${INSTALL_DIR}${RESET}"
echo -e "  Data dir:    ${CYAN}${DATA_DIR}${RESET}"
echo -e "  Service user:${CYAN}${SERVICE_USER}${RESET}"
echo -e "  Port:        ${CYAN}${BIND_PORT}${RESET}"

# ── Update only ─────────────────────────────────────────────────────────────
if $UPDATE_ONLY; then
  step "Updating…"
  # Fetch as the service user to avoid "dubious ownership" git errors when
  # the invoking user (root or a login user) does not own the repo.
  info "Fetching origin/main as $SERVICE_USER…"
  if ! sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch origin main 2>&1; then
    err "git fetch failed — check network and repo access"
    exit 1
  fi
  # Fast-forward attempt; fall back to reset --hard on a diverged (history-rewrite) clone.
  if ! sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" merge --ff-only origin/main 2>/dev/null; then
    warn "Fast-forward failed (diverged clone) — resetting to origin/main"
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" reset --hard origin/main
  fi
  ok "Source updated to $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
  # Install Python deps as service user
  sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
  # Build frontend as service user with a writable HOME (slop HOME=/nonexistent)
  sudo -u "$SERVICE_USER" env HOME="$(build_home)" \
    bash -c "cd '$INSTALL_DIR/frontend' && npm ci --silent && npm run build"
  ok "Frontend rebuilt → backend/static/"
  # Normalize ownership after update (fixes root-owned .git/FETCH_HEAD etc.)
  normalize_ownership "$INSTALL_DIR" "$SERVICE_USER"
  $SUDO systemctl restart slop
  ok "Updated and restarted."
  exit 0
fi

if $FRONTEND_ONLY; then
  step "Rebuilding frontend…"
  # Build as service user with a writable HOME (slop HOME=/nonexistent)
  sudo -u "$SERVICE_USER" env HOME="$(build_home)" \
    bash -c "cd '$INSTALL_DIR/frontend' && npm ci --silent && npm run build"
  ok "Frontend rebuilt → backend/static/"
  $SUDO systemctl restart slop
  ok "Frontend rebuilt and service restarted."
  exit 0
fi

# ── Step 1: Check dependencies ─────────────────────────────────────────────
step "1 / 8 — Checking system dependencies"

command -v python3 >/dev/null 2>&1 || die "Python 3.10+ required. Run: apt install python3 python3-venv"
PYVER=$(python3 -c "import sys; print(sys.version_info.minor)")
[[ "$PYVER" -ge 10 ]] || die "Python 3.10+ required (found 3.${PYVER})"
ok "Python 3.${PYVER}"

command -v node >/dev/null 2>&1 || die "Node.js 18+ required. Run: curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs"
NODE_VER=$(node --version | tr -d 'v' | cut -d. -f1)
[[ "$NODE_VER" -ge 18 ]] || die "Node.js 18+ required (found v${NODE_VER})"
ok "Node.js v$(node --version | tr -d 'v')"

command -v docker >/dev/null 2>&1 || die "Docker required. Run: curl -fsSL https://get.docker.com | sh"
ok "Docker $(docker --version | grep -oP '\d+\.\d+' | head -1)"

command -v rsync >/dev/null 2>&1 || {
  info "Installing rsync…"
  $SUDO apt-get install -y rsync -qq
}
ok "rsync"

# Ensure python3-venv is available
python3 -m venv --help >/dev/null 2>&1 || {
  info "Installing python3-venv…"
  $SUDO apt-get install -y python3-venv -qq
}

# ── Step 2: Create directories ─────────────────────────────────────────────
step "2 / 8 — Creating directories"

mkdir -p "$DATA_DIR"/{compose,logs}
mkdir -p "$DATA_DIR/.secrets"
chmod 700 "$DATA_DIR/.secrets"
ok "Data directory: $DATA_DIR"

# Normalize install-dir ownership to the service user
# normalize_ownership: chowns tree to svc_user:svc_user + re-asserts .env mode 600
normalize_ownership "$INSTALL_DIR" "$SERVICE_USER"
ok "Install directory: $INSTALL_DIR (owner: $SERVICE_USER)"

# ── Step 3: Python virtualenv ──────────────────────────────────────────────
step "3 / 8 — Installing Python dependencies"

if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
  info "Created virtualenv at $VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
ok "Python packages installed in virtualenv"

# ── Step 4: Build frontend ─────────────────────────────────────────────────
step "4 / 8 — Building frontend"

# Build as service user with a writable HOME (slop HOME=/nonexistent causes
# EACCES errors when npm tries to create ~/.npm). build_home returns ${MS_BUILD_HOME:-/tmp}.
sudo -u "$SERVICE_USER" env HOME="$(build_home)" \
  bash -c "cd '$INSTALL_DIR/frontend' && npm ci --silent && npm run build"
ok "Frontend built → backend/static/"

# ── Step 5: Create .env ────────────────────────────────────────────────────
step "5 / 8 — Environment file"

ENV_FILE="$INSTALL_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" << ENVEOF
# SLOP v3 — Configuration and secrets
# Edit this file, then run: sudo systemctl restart slop
#
# This file lives at: $ENV_FILE
# It is read directly by the service — no copying needed.

# ── Paths ──────────────────────────────────────────────────────────────────
CONFIG_ROOT=$INSTALL_DIR/config
MEDIA_ROOT=/mnt/media
TZ=America/Los_Angeles

# ── Docker ─────────────────────────────────────────────────────────────────
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 999)

# ── Service port ────────────────────────────────────────────────────────────
MS_PORT=${BIND_PORT}

# ── Data directory (state DB, compose fragments) ───────────────────────────
MS_DATA_DIR=${DATA_DIR}

# ── Cloudflare (required for HTTPS wildcard certs and tunnel) ──────────────
CF_DNS_API_TOKEN=
CF_ACCOUNT_ID=
CF_TUNNEL_ID=
CF_ZONE_ID=

# ── DNS provider credentials (uncomment the one matching your provider) ───
# Route53:
#AWS_ACCESS_KEY_ID=
#AWS_SECRET_ACCESS_KEY=
#AWS_REGION=us-east-1
#AWS_HOSTED_ZONE_ID=
# Namecheap:
#NAMECHEAP_API_USER=
#NAMECHEAP_API_KEY=
# Porkbun:
#PORKBUN_API_KEY=
#PORKBUN_SECRET_API_KEY=
# DigitalOcean:
#DO_AUTH_TOKEN=
# GoDaddy:
#GODADDY_API_KEY=
#GODADDY_API_SECRET=
# Hetzner:
#HETZNER_API_KEY=
# DuckDNS:
#DUCKDNS_TOKEN=
# Azure:
#AZURE_CLIENT_ID=
#AZURE_CLIENT_SECRET=
#AZURE_SUBSCRIPTION_ID=
#AZURE_RESOURCE_GROUP=
# Google Cloud DNS:
#GCE_PROJECT=
#GCE_SERVICE_ACCOUNT_FILE=
# Vultr:
#VULTR_API_KEY=
# Bunny:
#BUNNY_API_KEY=
# DNSPod:
#DNSPOD_API_KEY=
#DNSPOD_SECRET_ID=

# ── Shared services ─────────────────────────────────────────────────────────
POSTGRES_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

# #976 control-plane auth token — auto-provisioned at install so enforce mode has a real
# token to check. Masked in the Secrets UI (never returned in clear via the API); read the
# value here from this file when enabling enforce mode. The app also generate-if-absents this
# at startup, so existing installs get one on next restart; this seeds it visibly for new ones.
SLOP_CONTROL_PLANE_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# ── Optional ────────────────────────────────────────────────────────────────
PLEX_CLAIM=
KOMODO_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
KOMODO_PASSKEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
DOCKHAND_DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
ENVEOF
  chmod 600 "$ENV_FILE"
  $SUDO chown "$SERVICE_USER":"$SERVICE_USER" "$ENV_FILE"
  ok "Created: $ENV_FILE"
  warn "Fill in CF_DNS_API_TOKEN, CF_ZONE_ID, CF_ACCOUNT_ID before running the wizard"
else
  ok "Exists: $ENV_FILE"
fi

# ── Step 6: Systemd service ────────────────────────────────────────────────
step "6 / 8 — Installing systemd service"

# OPERATOR-ENV: .env is authoritative via EnvironmentFile=.
# Operator settings MS_TRUSTED_HOSTS and DOMAIN are read by the Python process
# via os.environ; they reach the process through the systemd EnvironmentFile=
# directive below. To change them: edit $INSTALL_DIR/.env then restart the service.
$SUDO tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=SLOP v3 — Self-hosted media stack manager
Documentation=https://github.com/SLOP-Platform/SLOP
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/python -m uvicorn backend.api.main:app \\
    --host 0.0.0.0 \\
    --port ${BIND_PORT} \\
    --log-level info \\
    --access-log
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

# Environment — single .env file, no copies
# Operator settings (MS_TRUSTED_HOSTS, DOMAIN, MS_PORT, etc.) are set in .env
# and loaded into the process environment via EnvironmentFile= below.
Environment=PYTHONPATH=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}

# Allow docker group access
SupplementaryGroups=docker

[Install]
WantedBy=multi-user.target
SVCEOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable slop
$SUDO systemctl restart slop
ok "Service installed and started (running as $SERVICE_USER)"

# ── Step 7: Install ms-update and ms-check ────────────────────────────────
step "7 / 8 — Installing ms-update, ms-check, and ms commands"

MS_UPDATE_SRC="$INSTALL_DIR/ms-update"
MS_UPDATE_LINK="/usr/local/bin/ms-update"
if [[ -f "$MS_UPDATE_SRC" ]]; then
  $SUDO ln -sf "$MS_UPDATE_SRC" "$MS_UPDATE_LINK"
  ok "Installed: ${CYAN}sudo ms-update${RESET}"
fi

MS_CHECK_SRC="$INSTALL_DIR/ms-check"
MS_CHECK_LINK="/usr/local/bin/ms-check"
if [[ -f "$MS_CHECK_SRC" ]]; then
  $SUDO ln -sf "$MS_CHECK_SRC" "$MS_CHECK_LINK"
  ok "Installed: ${CYAN}sudo ms-check${RESET}"
fi

# ms: the SLOP command-line client (cli/ms.py — a stdlib-only HTTP client to the
# local API). Symlinking it into /usr/local/bin means a plain `git pull` /
# `ms-update` auto-tracks the checkout (the link resolves to the freshly-pulled
# file), same as the probe below. cli/ms.py is executable with a python3 shebang
# and has no relative/backend imports, so it runs directly via the symlink.
MS_CLI_SRC="$INSTALL_DIR/cli/ms.py"
MS_CLI_LINK="/usr/local/bin/ms"
if [[ -f "$MS_CLI_SRC" ]]; then
  $SUDO ln -sf "$MS_CLI_SRC" "$MS_CLI_LINK"
  ok "Installed: ${CYAN}ms${RESET} (SLOP CLI)"
fi

# slop-reality-probe: the host-side GROUND probe the dev-time doc-vs-reality
# reconciler runs over ambient SSH (`ssh <host> slop-reality-probe`). It MUST be
# on the host PATH AND track the git checkout — symlinking it into /usr/local/bin
# means a plain `git pull` / `ms-update` auto-updates the PATH-resolved probe (the
# link resolves to the freshly-pulled file). Without this, the bare `ssh <host>
# slop-reality-probe` resolved a manually-placed STALE copy that never updated —
# the root cause of the recurring bound_port 8080-vs-22 false-DRIFT.
PROBE_SRC="$INSTALL_DIR/slop-reality-probe"
PROBE_LINK="/usr/local/bin/slop-reality-probe"
if [[ -f "$PROBE_SRC" ]]; then
  $SUDO ln -sf "$PROBE_SRC" "$PROBE_LINK"
  ok "Installed: ${CYAN}slop-reality-probe${RESET} (tracks the git checkout)"
fi

# ── Step 8: Smoke test + ms-check ─────────────────────────────────────────
step "8 / 8 — Smoke test + platform health check"
info "Waiting for API…"
for i in $(seq 1 20); do
  if curl -sf "${API_URL}/api/ping" >/dev/null 2>&1; then
    ok "API responding at ${API_URL}"
    break
  fi
  sleep 1
  [[ $i -eq 20 ]] && warn "API did not respond after 20s — check: journalctl -u slop -f"
done

# Run ms-check to validate the full install
if [[ -f "$INSTALL_DIR/ms-check" ]]; then
  info "Running platform health check…"
  bash "$INSTALL_DIR/ms-check" --quick || true  # non-fatal — shows warnings
fi

echo
echo -e "  ${BOLD}SLOP v3 deployed!${RESET}"
echo -e "  UI:     ${CYAN}${API_URL}${RESET}"
echo -e "  Logs:   ${CYAN}journalctl -u slop -f${RESET}"
echo -e "  Config: ${CYAN}${ENV_FILE}${RESET}"
echo
echo -e "  ${YELLOW}Next steps:${RESET}"
echo -e "  1. Fill in secrets: ${CYAN}nano ${ENV_FILE}${RESET}"
echo -e "  2. Open ${CYAN}${API_URL}${RESET} and run the setup wizard"
echo -e "  3. Deploy infrastructure slots (Auth, Tunnel)"
echo -e "  4. Install apps from the catalog"
echo
echo -e "  ${YELLOW}To update after a code push:${RESET}"
echo -e "  ${CYAN}sudo ms-update${RESET}"
echo
