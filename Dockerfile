# SLOP — Docker image (linux/amd64, modern desktop/server)
# Mount docker.sock and data directories at the SAME absolute path as the host.
# See docker-compose.yml for the correct volume mount convention.

# ── Frontend builder ────────────────────────────────────────────────────────
FROM node:22-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --quiet
COPY frontend/ ./
# Vite outDir is ../backend/static (frontend/vite.config.ts) — output lands at /app/backend/static/
RUN npm run build

# ── Python deps builder ─────────────────────────────────────────────────────
FROM python:3.12-slim AS python-builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Docker CLI source image ───────────────────────────────────────────────────
# Pinned to a concrete version tag (tracking id=846).
# TODO: upgrade to digest-pinned form once registry access is confirmed:
#   docker:28.0.1-cli@sha256:<digest>
FROM docker:28.0.1-cli AS docker-cli

# ── Runtime image ────────────────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates rsync sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Docker CLI + Compose plugin — SLOP shells out to `docker compose` to deploy
# Traefik (platform wizard) and every managed app (backend/core/compose.py).
# The Python SDK socket connection only covers reads/events, not management, so
# without these binaries the wizard and all app installs fail. Both are static
# Go binaries from the official docker:cli image, so they run on debian-slim.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# trivy — SLOP's runtime CVE probe (backend/agent/cve_audit.py) shells out to
# `trivy image` to scan managed app images + SLOP's own image for HIGH/CRITICAL
# vulnerabilities and emit health.cve findings. The probe degrades to INDETERMINATE
# (loud, never a false VERIFIED) when trivy is absent.
# NOTE: bundling the trivy static binary (+~100 MB) is DEFERRED pending a measured
# image-size diff — see tracking id=869. To enable, add a COPY --from of a
# version-pinned aquasec/trivy image (pin the tag/digest, not a mutable channel).

# Python packages + uvicorn from builder
COPY --from=python-builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=python-builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# Application code
COPY backend/ ./backend/
COPY catalog/ ./catalog/
# DB schema migrations — run_migrations() resolves _DEFAULT_MIGRATIONS_DIR to
# /app/migrations (backend/core/migrations.py). Without this the entrypoint's
# init_db() crash-loops with FileNotFoundError: '/app/migrations'.
COPY migrations/ ./migrations/
COPY --from=frontend-builder /app/backend/static/ ./backend/static/
COPY docker-entrypoint.sh /entrypoint.sh

# Runtime environment
ENV PYTHONPATH=/app
ENV MS_DATA_DIR=/srv/slop/data
ENV MS_CONFIG_ROOT=/srv/slop/config
ENV MS_HOST_DATA_DIR=/srv/slop/data
ENV MS_HOST_CONFIG_DIR=/srv/slop/config
ENV MS_HOST_ENV_FILE=/srv/slop/.env
# config.env_file (backend/core/config.py:138) reads MS_ENV_FILE — NOT
# MS_HOST_ENV_FILE (which is set but read nowhere). Without MS_ENV_FILE it
# defaults to install_dir/.env = /app/.env, which is INSIDE the container:
# ephemeral (wiped on recreate) and unmounted. App deploys then run
# `docker compose --env-file /app/.env up` and fail "couldn't find env file".
# Point it at the mounted, persistent host .env so edits + wizard writes survive.
ENV MS_ENV_FILE=/srv/slop/.env

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:8080/api/ping || exit 1

ENTRYPOINT ["/entrypoint.sh"]
