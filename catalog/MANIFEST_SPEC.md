# SLOP App Manifest Specification
# Version: 1.0
#
# Every catalog entry (apps/ and infra/) is a YAML file following this spec.
# The manifest drives ALL automation: deploy, wire, health check, self-heal,
# remove, and CF hostname registration.
#
# Required fields are marked [required]. Everything else has a default.
# Community manifests must also follow this spec to be accepted into registry/.

# ─────────────────────────────────────────────────────────────────────────────
# Identity
# ─────────────────────────────────────────────────────────────────────────────

key: sonarr                         # [required] unique ID, lowercase, no spaces
display_name: Sonarr                # [required] human label shown in UI
version: "1.0"                      # manifest spec version (always "1.0" for now)
description: TV series manager      # one-line description
category: arr                       # [required] arr|media|downloader|requests|tools|ai|infra
tier: 2                             # 1=infrastructure, 2=app (default: 2)
icon: 📺                            # emoji icon shown in UI


# ─────────────────────────────────────────────────────────────────────────────
# Container
# ─────────────────────────────────────────────────────────────────────────────

image: lscr.io/linuxserver/sonarr   # [required] Docker image (no tag — tag managed separately)
image_tag: latest                   # default tag; user can override
linuxserver: true                   # inject PUID, PGID, TZ env vars (default: true)
                                    # set false for non-linuxserver images (node, etc.)


# ─────────────────────────────────────────────────────────────────────────────
# Ports
# ─────────────────────────────────────────────────────────────────────────────

ports:
  web: 8989                         # [required if web-accessible] internal container port
  # extra:                          # additional ports (e.g. Tailscale, torrent)
  #   - name: bt
  #     internal: 6881
  #     protocol: tcp               # tcp | udp (default: tcp)


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

volumes:
  config: /config                   # [required] config dir inside container
                                    # host path = platform.config_root / key
  media: /data                      # optional: mount platform.media_root here
  # custom:                         # arbitrary extra mounts
  #   - host: /host/path
  #     container: /container/path
  #     readonly: false


# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────

env: {}                             # static env vars (values, not secrets)
                                    # secrets go in .env and are referenced as ${VAR}
# env:
#   WEB_PORT: "8085"                # example: SABnzbd needs this to match the port


# ─────────────────────────────────────────────────────────────────────────────
# Dependencies
# ─────────────────────────────────────────────────────────────────────────────

# dependencies:
#   postgres: true                  # auto-deploy shared PostgreSQL, create DB + user
#   redis: true                     # auto-deploy shared Valkey/Redis
#   apps:                           # other apps that must be INSTALLED (exist in DB) first
#     - prowlarr                    # note: "installed" means the app's DB record exists, NOT that
#                                   # it is currently running. An installed-but-stopped dep still
#                                   # has a stable URL and service name. Only when the dep has
#                                   # never been installed at all is wiring deferred.


# ─────────────────────────────────────────────────────────────────────────────
# Traefik / external access
# ─────────────────────────────────────────────────────────────────────────────

traefik:
  enabled: true                     # generate Traefik labels (default: true)
  subdomain: sonarr                 # subdomain prefix (default: key)
  # skip: false                     # true for infra-only services with no web UI

# Special Traefik header requirements (e.g. BentoPDF needs COOP/COEP)
# traefik_headers: {}


# ─────────────────────────────────────────────────────────────────────────────
# Wiring — automated inter-app connections
# ─────────────────────────────────────────────────────────────────────────────

# wiring:
#   accepts:                        # this app CAN receive connections from:
#     - type: indexer               # wire type label
#       from: prowlarr              # which app initiates the connection
#       description: Prowlarr registers indexers via Sonarr API
#   connects_to:                    # this app initiates connections to:
#     - type: notification
#       to: plex
#       description: Notify Plex to refresh library on import
#       optional: true              # don't fail install if target not present


# ─────────────────────────────────────────────────────────────────────────────
# Post-deploy steps
# ─────────────────────────────────────────────────────────────────────────────

# post_deploy:
#   - type: wait_healthy            # wait for container to report healthy
#     timeout: 60                   # seconds
#
#   - type: api_ready               # poll the API until it responds
#     path: /api/v3/system/status   # endpoint to poll
#     timeout: 120
#
#   - type: wire                    # trigger wiring with another app
#     target: prowlarr
#     wire_type: indexer


# ─────────────────────────────────────────────────────────────────────────────
# Health checks
# ─────────────────────────────────────────────────────────────────────────────

# health:
#   checks:
#     - name: api_reachable
#       type: http                  # http | tcp | process | custom
#       path: /api/v3/system/status # for http checks
#       expect_status: 200
#       interval: 30                # seconds between checks
#
#   self_heal:
#     - condition: api_unreachable  # matches a check name
#       action: restart             # restart | rewire | notify
#       max_attempts: 3
#       cooldown: 60                # seconds between heal attempts


# ─────────────────────────────────────────────────────────────────────────────
# Remove behaviour
# ─────────────────────────────────────────────────────────────────────────────

# remove:
#   ask_delete_config: true         # prompt before deleting config folder (default: true)
#   cleanup_wiring: true            # remove wiring entries in connected apps (default: true)
#   cleanup_cf_hostnames: true      # remove CF Tunnel hostname if registered (default: true)


# ─────────────────────────────────────────────────────────────────────────────
# GPU (for AI/transcoding apps)
# ─────────────────────────────────────────────────────────────────────────────

# gpu:
#   optional: true                  # allow install without GPU (CPU-only mode)
#   warn_if_absent: true            # show warning if no GPU detected
#   nvidia: true                    # request NVIDIA runtime if available
#   amd: false                      # request AMD ROCm if available


# ─────────────────────────────────────────────────────────────────────────────
# Startup timing
# ─────────────────────────────────────────────────────────────────────────────

# start_grace_s: 120                # Legacy: seconds after container start before health failures
#                                   # are surfaced. 0 = use the Docker default (120s).
#                                   # Kept for backward compatibility — use health_grace_s instead
#                                   # when you need to set this explicitly.

# install_wait_s: 120               # Patience budget (seconds) for wait_healthy / api_ready
#                                   # post_deploy steps. These steps are handled by the health
#                                   # scheduler (not inline during install), so this sets how
#                                   # long the scheduler will wait before reporting failure for
#                                   # a newly-deployed app.
#                                   # Falls back to start_grace_s when not set.
#                                   # Use this when the app has a long first-run initialisation
#                                   # (e.g. database migrations, model downloads) that is separate
#                                   # from the ongoing health grace window.

# health_grace_s: 120               # How long (seconds) the health scheduler suppresses failures
#                                   # for a newly-started or just-restarted app. Prevents false
#                                   # alerts during normal startup.
#                                   # Falls back to start_grace_s when not set.
#                                   # Use this when the app needs a long grace window for the
#                                   # health scheduler specifically (e.g. media servers, AI apps)
#                                   # but install_wait_s can be shorter.


# ─────────────────────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────────────────────

# links:
#   docs: https://wiki.servarr.com/sonarr
#   github: https://github.com/Sonarr/Sonarr

# tags:                             # for catalog search / filtering
#   - arr
#   - tv
#   - media-management


# ─────────────────────────────────────────────────────────────────────────────
# Adding a new wire_type
# ─────────────────────────────────────────────────────────────────────────────
#
# 1. Implement a handler function in `backend/manifests/wiring.py` with signature:
#      def my_handler(source_key: str, source_manifest: Any, config_root: str) -> str
#    The return value must be one of: "wired", "deferred", or "failed".
#      "wired"    — configuration confirmed in the target app
#      "deferred" — a prerequisite isn't ready yet; health scheduler retries later
#      "failed"   — handler failed; row is marked failed and surfaced to the UI
#
# 2. Register the handler at module level in wiring.py (after the function):
#      WIRE_HANDLERS["your_type"] = your_handler
#
# The loader warns at manifest load time when a post_deploy wire step references
# a wire_type with no registered handler, so problems are caught early.
