# Migration guides

This file covers these migration paths:

- [path migration](#upgrading-from-pre-batch-27-installations-mediastack--slop-path-migration) — rename old install/data/service to the new `slop` layout
- [v2 → v3](#migrating-from-mediastack-rad-v2--v3) — msrad to the current platform
- [v3 → v4](#migrating-from-mediastack-v3-systemd-to-v4-docker) — systemd to Docker

---

## Upgrading from pre-v1.0 installations (mediastack → slop path migration)

Earlier releases installed under `/opt/mediastack`, stored data in
`/var/lib/mediastack`, and ran a `mediastack.service` systemd unit owned by a
`mediastack` system user. Current releases use the `slop` names instead. If you
have an existing install, migrate it in place with these steps:

1. Stop the running service:

   ```bash
   sudo systemctl stop mediastack.service
   ```

2. Move the data directory:

   ```bash
   sudo mv /var/lib/mediastack /var/lib/slop
   ```

3. Move the install directory:

   ```bash
   sudo mv /opt/mediastack /opt/slop
   ```

4. Remove the old service unit:

   ```bash
   sudo systemctl disable mediastack.service && sudo rm /etc/systemd/system/mediastack.service
   ```

5. Re-run the installer to install the new `slop.service` unit and the `slop`
   system user against the relocated directories.

6. Enable and start the new service:

   ```bash
   sudo systemctl enable slop.service && sudo systemctl start slop.service
   ```

---

# Migrating from Mediastack-RAD v2 → v3

## Prerequisites

Before running the migration script, ensure:

```bash
# PyYAML is required to parse the v2 docker-compose.yml
# (already in requirements.txt — install if running outside the venv)
pip install pyyaml

# Mediastack v3 must be running
./ms status   # or: curl http://localhost:8080/api/ping
```

Without PyYAML installed, the script falls back to inspecting running Docker
containers via `docker ps`. This fallback is less reliable — it can only detect
containers that are currently running and may miss stopped services or produce
incorrect names on non-standard compose project names. Install PyYAML first.

## Overview

v2 (`/opt/msrad`, commit `30c52f1`) and v3 can run **simultaneously** on
the same server. Migration is non-destructive: v3 installs apps fresh, v2 keeps
running until you're satisfied and switch over.

## v2 stack → v3 manifest mapping

| v2 compose service | v3 key          | Notes |
|--------------------|-----------------|-------|
| traefik            | *(platform)*    | Platform wizard deploys Traefik |
| cloudflared        | *(infra slot)*  | Deploy via Infrastructure → Tunnel |
| tinyauth           | *(infra slot)*  | Deploy via Infrastructure → Auth |
| sonarr             | `sonarr`        | Direct config folder re-use |
| radarr             | `radarr`        | Direct config folder re-use |
| prowlarr           | `prowlarr`      | Direct config folder re-use |
| bazarr             | `bazarr`        | Direct config folder re-use |
| overseerr          | `overseerr`     | Config folder re-use |
| sabnzbd            | `sabnzbd`       | Config folder re-use |
| qbittorrent        | `qbittorrent`   | Config folder re-use |

## Step-by-step migration

### 1. Run the v3 platform wizard

```bash
cd /opt/mediastack  # v3 repo
./ms wizard
```

Use your existing domain and config paths. The wizard deploys a new Traefik
instance on ports 81/444 (different from v2's 80/443) by default, so both
proxy stacks can coexist temporarily.

### 2. Deploy infra slots

Open the v3 UI at http://localhost:8080, go to Infrastructure, and deploy:
- **Auth** → TinyAuth (same version as v2)
- **Tunnel** → Cloudflare Tunnel (use your existing tunnel token)

### 3. Migrate app config (optional — recommended)

v3 app containers use the same config structure as v2. You can point v3 apps
at the existing v2 config directories to avoid re-configuring from scratch:

```bash
# Example: use v2 Sonarr config in v3
curl -X POST http://localhost:8080/api/apps/sonarr/install \
  -H "Content-Type: application/json" \
  -d '{"extra_env": {}}'
```

v3's `config_root` setting determines where config folders are created.
Set it to match v2's config path during the wizard, and apps will find
their existing databases automatically.

### 4. Run the migration script (automated)

```bash
python3 tools/migrate_from_v2.py \
  --v2-path /opt/msrad \
  --api-url http://localhost:8080 \
  --dry-run
```

Remove `--dry-run` to actually install the detected apps.

### 5. Verify v3 apps are healthy

```bash
./ms health status
./ms apps list
```

### 6. Switch DNS / Cloudflare Tunnel

Once v3 apps are verified, update your Cloudflare Tunnel to point at the v3
Traefik instance instead of v2. Traffic switches instantly, no downtime.

### 7. Stop v2

```bash
cd /opt/msrad
docker compose down
```

## Running the migration script

```
usage: migrate_from_v2.py [-h] [--v2-path PATH] [--api-url URL] [--dry-run]

optional arguments:
  --v2-path PATH   Path to v2 msrad directory (default: /opt/msrad)
  --api-url URL    Mediastack v3 API URL (default: http://localhost:8080)
  --dry-run        Show what would be installed without doing it
```

## Config compatibility

| App | Config path | Compatible? |
|-----|-------------|-------------|
| Sonarr | `/config/sonarr` | ✅ Full compatibility |
| Radarr | `/config/radarr` | ✅ Full compatibility |
| Prowlarr | `/config/prowlarr` | ✅ Full compatibility |
| Bazarr | `/config/bazarr` | ✅ Full compatibility |
| Overseerr | `/config/overseerr` | ✅ Full compatibility |
| SABnzbd | `/config/sabnzbd` | ✅ Full compatibility |
| qBittorrent | `/config/qbittorrent` | ✅ Full compatibility |

---

# Migrating from Mediastack v3 (systemd) to v4 (Docker)

## What changed in v4

Mediastack v4 ships as a Docker image. No Python, Node, or git required on
the host. Updates are a `docker compose pull && docker compose up -d`.

The systemd deployment continues to work — `ms-update` still functions.
v4 is the **recommended** path for new installs and for anyone who wants
simpler updates going forward.

---

## Migrating an existing v3 systemd install

### 1. Verify your data directory

Your data is in one of:
- `/srv/mediastack/data/` (standard)
- `/data/mediastack/` (older layout — run `sudo ms-check` to confirm)

The data directory contains `state.db` and `compose/`. **This data migrates automatically** — no export needed.

### 2. Stop the systemd service

```bash
sudo systemctl stop mediastack
sudo systemctl disable mediastack
```

### 3. Create the Docker compose file

```bash
cd /srv/mediastack
curl -fsSL https://raw.githubusercontent.com/Nnyan/SLOP/main/docker-compose.yml \
  -o docker-compose.yml
```

### 4. Verify your .env

Your existing `/srv/mediastack/.env` works as-is. Confirm it has:
```
DOMAIN=yourdomain.com
CF_DNS_API_TOKEN=...
POSTGRES_PASSWORD=...
```

### 5. Start the Docker container

```bash
docker compose up -d
docker compose logs -f mediastack   # watch startup
```

### 6. Verify

Open http://your-server-ip:8080 — your apps, health data, and settings are all intact.

### 7. Remove the systemd service files (optional)

```bash
sudo rm /etc/systemd/system/mediastack.service
sudo systemctl daemon-reload
```

---

## Volume mount contract

The compose file mounts data at the **same absolute path** inside and outside
the container. This is required because Mediastack writes compose fragments
containing host paths like `/srv/mediastack/config/sonarr:/config` — the
Docker daemon reads these from the HOST filesystem.

```yaml
volumes:
  - /srv/mediastack/data:/srv/mediastack/data    # identical paths
  - /srv/mediastack/config:/srv/mediastack/config
```

If you use a different base path, update both sides of the volume mount AND
set `MS_HOST_DATA_DIR` and `MS_HOST_CONFIG_DIR` in the environment.

---

## Updating v4

```bash
cd /srv/mediastack
docker compose pull
docker compose up -d
```

No git, no pip, no service restart needed.
