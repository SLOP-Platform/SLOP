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
# Tag-pinned to the multi-arch `docker:29-cli` (tracking id=846; bumped #1149). The
# prior docker:28.0.1-cli base carried 7 CRITICAL + 94 HIGH CVEs (Go stdlib/containerd/
# docker/buildkit modules built on Go 1.23.6); 29-cli cleared all 7 CRITICAL.
# SHIPPED SURFACE (#1221, trivy 0.71.0 re-scan 2026-06-21): only the `docker` +
# `docker-compose` Go binaries are COPYed into the final image (below) — builder-stage
# OS pkgs and the `docker-buildx` plugin do NOT ship. Per shipped binary: `docker` CLI
# = 0 CVE (clean); `docker-compose` plugin = 6 HIGH (containerd/v2, docker/docker, Go
# stdlib). The gate scans the whole `docker:29-cli` FROM image so it ALSO flags buildx
# (4 HIGH) + the alpine OS pkgs, which are NOT in the shipped artifact set.
# The 6 shipped docker-compose HIGHs now HAVE upstream fixes (containerd 2.3.2, docker
# 29.3.1, Go stdlib 1.26.4) — but no rebuilt docker:*-cli or docker/compose-bin image
# has absorbed them yet (compose-bin:latest is identically 6 HIGH; docker:cli=4 HIGH,
# docker:28-cli=21 — no tag is green; node:22/24/lts-slim each red on a fresh npm HIGH).
# No base bump remediates today; surfaced (non-blocking) by the trivy base-image CVE
# gate in .github/workflows/security-lint.yml (#1149/#836). RE-EVAL TRIGGER: re-scan +
# bump when a docker:*-cli tag ships docker>=29.3.1 + containerd>=2.3.2 + Go>=1.26.4.
# NOTE: kept a TAG, not a digest, because the docker.yml publish workflow builds linux/amd64+arm64
# and a platform-specific digest would break the arm64 leg; the original 28.0.1-cli pin
# was a tag for the same reason. id=846's digest-pin TODO remains open pending a
# confirmed multi-arch INDEX digest.
FROM docker:29-cli AS docker-cli

# ── Runtime image ────────────────────────────────────────────────────────────
FROM python:3.12-slim

# hadolint ignore=DL3008  # justified: pinning these system pkgs on the MOVING python:3.12-slim base is brittle (a base bump can drop a pinned version → build break); the base is the version anchor. Registered in tools/suppression_ledger.json (#1134).
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
# NOTE: bundling the trivy static binary is DECLINED — id=869 GROUND-measured the real
# delta on the self-hosted test runner (docker 29.5.2): the trivy 0.71 binary is ~161 MiB uncompressed
# (~58 MiB compressed), ~40% above the old +100 MB estimate, ~doubling SLOP's lean image.
# host-trivy + loud-INDETERMINATE is the standing control (ADR 0024 FROM-audit posture).
# See docs/AGENT-869-TRIVY-BUNDLE-SIZE-CONFIRM.md. If ever revisited, budget ~161 MiB
# uncompressed and add a COPY --from of a version-pinned aquasec/trivy image (pin tag/digest).

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

ARG BUILD_DATE
ARG VCS_REF
LABEL org.opencontainers.image.created=$BUILD_DATE \
      org.opencontainers.image.revision=$VCS_REF

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
