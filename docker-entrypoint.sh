#!/bin/bash
# docker-entrypoint.sh — SLOP container startup
set -euo pipefail

DATA_DIR="${MS_DATA_DIR:-/srv/slop/data}"
CONFIG_DIR="${MS_CONFIG_ROOT:-/srv/slop/config}"

echo "→ SLOP starting up…"
echo "  Data dir:   $DATA_DIR"
echo "  Config dir: $CONFIG_DIR"

# ── Create required directories ────────────────────────────────────────────
mkdir -p "$DATA_DIR/compose" "$CONFIG_DIR"

# ── Wait for docker.sock ────────────────────────────────────────────────────
SOCK="${DOCKER_HOST:-/var/run/docker.sock}"
SOCK="${SOCK#unix://}"
echo "→ Waiting for Docker socket at $SOCK…"
for i in $(seq 1 30); do
    if [ -S "$SOCK" ]; then
        echo "  ✓ Docker socket ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  ✗ Docker socket not found after 30s — check volume mount"
        exit 1
    fi
    sleep 1
done

# ── Initialise / migrate database ─────────────────────────────────────────
echo "→ Initialising database…"
python3 -c "
import sys
sys.path.insert(0, '/app')
from pathlib import Path
from backend.core.state import configure, init_db
db = Path('${DATA_DIR}/state.db')
configure(db)
init_db(db)
print('  ✓ Database ready')
"

# ── Start application ──────────────────────────────────────────────────────
echo "→ Starting SLOP v4…"
exec python3 -m uvicorn backend.api.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 1 \
    --log-level info
