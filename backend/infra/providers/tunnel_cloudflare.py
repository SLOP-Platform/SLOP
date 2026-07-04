"""backend/infra/providers/tunnel_cloudflare.py

Cloudflare Tunnel provider.

Implements: deploy, remove, verify, register_hostname, unregister_hostname,
list_hostnames.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from backend.core import docker_client
from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult

log = get_logger(__name__)

CONTAINER_NAME = "cloudflared"
IMAGE = "cloudflare/cloudflared:latest"
CF_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareTunnelProvider(InfraProvider):
    slot = "tunnel"
    key = "cloudflared"
    display_name = "Cloudflare Tunnel"

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy the Cloudflare Tunnel container.

        Required config:
          tunnel_token  — from CF Zero Trust → Networks → Tunnels
          domain        — base domain
        Optional:
          auto_register — bool, whether to auto-register hostnames on app install
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name
        tunnel_token = cfg.get("tunnel_token", "${CF_TUNNEL_TOKEN}")
        auto_register = cfg.get("auto_register", False)
        domain = cfg.get("domain") or platform.domain or ""

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "networks": [network],
            "command": f"tunnel --no-autoupdate run --token {tunnel_token}",
            "environment": {
                "TUNNEL_TOKEN": tunnel_token,
            },
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure(
                    "Cloudflare Tunnel failed to start.",
                    _out[:300],
                )
        except Exception as e:
            return ProviderResult.failure("Could not deploy Cloudflare Tunnel.", str(e))

        with StateDB() as db:
            db.upsert_tunnel_provider(
                "cloudflared",
                status="active",
                config={
                    "domain": domain,
                    "auto_register": auto_register,
                },
                deployed_at=int(time.time()),
            )
            db.set_setting("cf_auto_register_hostnames", "true" if auto_register else "false")

        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "cloudflared",
                    display_name="Cloudflare Tunnel",
                    tier=0,  # tier 0 = infrastructure layer
                    category="networking",
                    status="running",
                    image=IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=None,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            "Cloudflare Tunnel deployed.",
            data={"auto_register": auto_register, "domain": domain},
        )

    def remove(self) -> ProviderResult:
        from backend.core.compose import remove_fragment

        frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
        if frag_path.exists():
            try:
                compose_down(frag_path, timeout=30)
            except Exception as e:
                return ProviderResult.failure("Could not stop Cloudflare Tunnel.", str(e))
        remove_fragment(CONTAINER_NAME)
        with StateDB() as db:
            db.remove_tunnel_provider("cloudflared")
        return ProviderResult.success("Cloudflare Tunnel removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if c is None:
            return ProviderResult.failure(
                "Cloudflared container is not running.",
                "Deploy Cloudflare Tunnel via the infrastructure wizard.",
            )
        if c.status != "running":
            return ProviderResult.failure(
                f"Cloudflared is in '{c.status}' state.",
                f"Check logs: docker logs {CONTAINER_NAME}",
            )
        return ProviderResult.success("Cloudflare Tunnel is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        """Register a hostname in the CF Tunnel via the CF API.

        Reads the current tunnel ingress config first, then appends (or updates)
        the new hostname rule before writing back. This preserves all existing
        ingress rules — the old approach overwrote them with a single-rule payload.

        Requires CF_DNS_API_TOKEN set in .env with:
          - Cloudflare Tunnel: Edit  |  Zone: DNS: Edit  |  Zone: Zone: Read
        """
        token, account_id, tunnel_id, zone_id = self._load_cf_credentials()
        if not all([token, account_id, tunnel_id, zone_id]):
            return ProviderResult.failure(
                "CF API credentials not configured.",
                "Set CF_DNS_API_TOKEN, CF_ACCOUNT_ID, CF_TUNNEL_ID, and CF_ZONE_ID "
                "in Settings → Secrets.",
            )

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 1. GET current tunnel ingress to preserve existing rules
        try:
            get_r = httpx.get(
                f"{CF_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
                headers=headers,
                timeout=15,
            )
            if get_r.is_success:
                existing = get_r.json().get("result", {}).get("config", {}).get("ingress", [])
                # Drop the catch-all terminator and any stale rule for this hostname
                rules = [r for r in existing if r.get("hostname") and r["hostname"] != hostname]
            else:
                log.warning(
                    "Could not fetch tunnel config (%d) — starting fresh.", get_r.status_code
                )
                rules = []
        except Exception as e:
            log.warning("Could not fetch tunnel config: %s — starting fresh.", e)
            rules = []

        # 2. Rebuild ingress: new rule first, existing rules, catch-all last
        ingress = [
            {"hostname": hostname, "service": target},
            *rules,
            {"service": "http_status:404"},
        ]

        # 3. PUT the full updated configuration
        try:
            put_r = httpx.put(
                f"{CF_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
                headers=headers,
                json={"config": {"ingress": ingress}},
                timeout=15,
            )
            if not put_r.is_success:
                return ProviderResult.failure(
                    f"Could not add {hostname} to tunnel.", put_r.text[:300]
                )
            log.info("CF Tunnel updated: %d ingress rules", len(ingress))
        except Exception as e:
            return ProviderResult.failure(f"CF API error for {hostname}.", str(e))

        # 4. Create or update proxied CNAME record in CF DNS
        dns_id = None
        try:
            existing_dns = (
                httpx.get(
                    f"{CF_API_BASE}/zones/{zone_id}/dns_records",
                    headers=headers,
                    params={"name": hostname, "type": "CNAME"},
                    timeout=10,
                )
                .json()
                .get("result", [])
            )
            cname = {
                "type": "CNAME",
                "name": hostname,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True,
            }
            if existing_dns:
                dns_r = httpx.put(
                    f"{CF_API_BASE}/zones/{zone_id}/dns_records/{existing_dns[0]['id']}",
                    headers=headers,
                    json=cname,
                    timeout=10,
                )
            else:
                dns_r = httpx.post(
                    f"{CF_API_BASE}/zones/{zone_id}/dns_records",
                    headers=headers,
                    json=cname,
                    timeout=10,
                )
            dns_id = dns_r.json().get("result", {}).get("id")
        except Exception as e:
            return ProviderResult.failure(f"Could not create DNS record for {hostname}.", str(e))

        with StateDB() as db:
            db.record_external_resource(
                "cf_tunnel_hostname",
                hostname=hostname,
                target=target,
                resource_id=dns_id,
                config={"tunnel_id": tunnel_id, "zone_id": zone_id},
            )

        return ProviderResult.success(
            f"Hostname {hostname} registered in CF Tunnel → {target}. "
            f"{len(ingress) - 1} total rules.",
            data={"hostname": hostname, "dns_id": dns_id, "total_rules": len(ingress)},
        )

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        token, account_id, tunnel_id, zone_id = self._load_cf_credentials()
        if not all([token, account_id, tunnel_id]):
            return ProviderResult.failure(
                "CF API credentials not configured — remove the hostname manually "
                "in Cloudflare Zero Trust → Networks → Tunnels.",
            )

        with StateDB() as db:
            resources = db.get_active_resources()
        matching = [r for r in resources if r["hostname"] == hostname and r["resource_id"]]

        if not matching:
            return ProviderResult.success(
                f"No CF resource found for {hostname} — may have been removed already."
            )

        headers = {"Authorization": f"Bearer {token}"}
        errors = []
        for res in matching:
            if res.get("resource_id") and zone_id:
                try:
                    httpx.delete(
                        f"{CF_API_BASE}/zones/{zone_id}/dns_records/{res['resource_id']}",
                        headers=headers,
                        timeout=10,
                    )
                except Exception as e:
                    errors.append(str(e))
            with StateDB() as db:
                db.mark_resource_removed(hostname)

        if errors:
            return ProviderResult.failure(
                f"Hostname {hostname} partially removed. Some DNS records may remain.",
                "\n".join(errors),
            )
        return ProviderResult.success(f"Hostname {hostname} unregistered.")

    def list_hostnames(self) -> ProviderResult:
        with StateDB() as db:
            resources = db.get_active_resources()
        hostnames = [r["hostname"] for r in resources if r["resource_type"] == "cf_tunnel_hostname"]
        return ProviderResult.success(
            f"{len(hostnames)} active CF hostname(s).",
            data={"hostnames": hostnames},
        )

    def pre_migration_snapshot(self) -> ProviderResult:
        hostnames = self.list_hostnames()
        with StateDB() as db:
            provider_rec = db.get_tunnel_provider("cloudflared") or {}
        return ProviderResult.success(
            "CF Tunnel snapshot captured.",
            data={"slot": provider_rec, "hostnames": hostnames.data or {}},
        )

    def _load_cf_credentials(self) -> tuple[str | None, str | None, str | None, str | None]:
        """Load CF credentials from .env file. Returns (token, account_id, tunnel_id, zone_id)."""
        env_file = config.data_dir / ".env"
        env: dict[str, str] = {}
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
        return (
            env.get("CF_DNS_API_TOKEN", ""),
            env.get("CF_ACCOUNT_ID", ""),
            env.get("CF_TUNNEL_ID", ""),
            env.get("CF_ZONE_ID", ""),
        )

    def register_dns_only_record(self, hostname: str) -> ProviderResult:
        """Create a DNS-only (unproxied) A record for media server direct access.

        Media apps (Plex, Jellyfin, Emby) cannot route through CF Tunnel/CDN
        due to ToS restrictions on video streaming. Instead they need a
        DNS-only A record pointing to the server's public IP.

        The record is created as unproxied (gray cloud) so CF does not
        inspect or route the traffic — the connection goes directly to
        the server where Traefik terminates TLS with the wildcard cert.

        NOTE: The A record points to the server's current public IP.
        Install ddns-updater to keep it current on dynamic IP connections.
        """
        token, _, _, zone_id = self._load_cf_credentials()
        if not all([token, zone_id]):
            return ProviderResult.failure(
                "CF API credentials not configured.",
                "Set CF_DNS_API_TOKEN and CF_ZONE_ID in Settings → Secrets.",
            )

        # Detect public IP
        public_ip = self._get_public_ip()
        if not public_ip:
            return ProviderResult.failure(
                f"Could not determine server public IP for {hostname}.",
                "Set the A record manually in Cloudflare DNS (DNS only, gray cloud).",
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            # Check if record already exists
            existing = httpx.get(
                f"{CF_API_BASE}/zones/{zone_id}/dns_records",
                headers=headers,
                params={"name": hostname, "type": "A"},
                timeout=10,
            )
            existing_records = existing.json().get("result", [])

            if existing_records:
                # Update existing record
                record_id = existing_records[0]["id"]
                r = httpx.put(
                    f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}",
                    headers=headers,
                    json={
                        "type": "A",
                        "name": hostname,
                        "content": public_ip,
                        "proxied": False,  # DNS only — no CF CDN, required for media
                        "ttl": 300,
                    },
                    timeout=10,
                )
            else:
                # Create new record
                r = httpx.post(
                    f"{CF_API_BASE}/zones/{zone_id}/dns_records",
                    headers=headers,
                    json={
                        "type": "A",
                        "name": hostname,
                        "content": public_ip,
                        "proxied": False,  # DNS only — required for media streaming
                        "ttl": 300,
                    },
                    timeout=10,
                )

            if not r.is_success:
                return ProviderResult.failure(
                    f"Could not create DNS-only A record for {hostname}.",
                    r.text[:300],
                )

            dns_id = r.json().get("result", {}).get("id")
            with StateDB() as db:
                db.record_external_resource(
                    "cf_dns_a_record",
                    hostname=hostname,
                    target=public_ip,
                    resource_id=dns_id,
                    config={"proxied": False, "zone_id": zone_id},
                )

            return ProviderResult.success(
                f"DNS-only A record created: {hostname} → {public_ip}. "
                f"Media server accessible directly (no CF CDN — ToS compliant). "
                f"Install ddns-updater if your home IP changes.",
                data={"hostname": hostname, "ip": public_ip, "dns_id": dns_id},
            )

        except Exception as e:
            return ProviderResult.failure(
                f"CF API error creating DNS record for {hostname}.",
                str(e),
            )

    @staticmethod
    def _get_public_ip() -> str | None:
        """Detect the server's current public IP via a reliable external service."""
        for url in [
            "https://api.ipify.org",
            "https://icanhazip.com",
            "https://ifconfig.me/ip",
        ]:
            try:
                r = httpx.get(url, timeout=5)
                ip = r.text.strip()
                if ip and "." in ip:  # basic IPv4 sanity check
                    return ip
            except Exception:  # noqa: S112 — skip unreachable/erroring IP-echo services and try the next fallback
                continue
        return None
