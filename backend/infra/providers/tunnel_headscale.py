"""backend/infra/providers/tunnel_headscale.py

Headscale — self-hosted Tailscale control server.
Provides a private WireGuard mesh network without relying on Tailscale's infrastructure.
"""

from __future__ import annotations
from typing import Any, ClassVar

from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

log = get_logger(__name__)
CONTAINER_NAME = "headscale"


@register
class HeadscaleProvider(InfraProvider):
    slot = "tunnel"
    key = "headscale"
    display_name = "Headscale"
    category = "networking"
    description = (
        "Self-hosted Tailscale control server. Full WireGuard mesh — no Tailscale cloud dependency."
    )

    fields: ClassVar = [
        {
            "key": "domain",
            "label": "Headscale domain",
            "type": "text",
            "placeholder": "headscale.example.com",
            "required": True,
            "help": "Public domain where Headscale will be reachable. Must be publicly accessible.",
        },
        {
            "key": "noise_private_key",
            "label": "Noise private key path",
            "type": "text",
            "placeholder": "/etc/headscale/noise_private.key",
            "required": False,
            "help": "Path to the Noise protocol private key. Auto-generated if blank.",
        },
        {
            "key": "ip_prefixes",
            "label": "IP prefix",
            "type": "text",
            "placeholder": "100.64.0.0/10",
            "required": False,
            "help": "CGNAT IP range for the mesh network. Default: 100.64.0.0/10",
        },
        {
            "type": "info",
            "key": "_info",
            "label": "",
            "help": (
                "After deployment, create users with: docker exec headscale headscale users create admin. "
                "Generate auth keys with: docker exec headscale headscale preauthkeys create --user admin. "
                "Connect clients with: tailscale up --login-server https://headscale.example.com"
            ),
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        domain = cfg.get("domain", "").strip()
        if not domain:
            return ProviderResult.failure("Domain is required.", "")

        with StateDB() as db:
            _p = db.get_platform()
        cert_resolver = _p.cert_resolver or "letsencrypt"

        ip_prefixes = cfg.get("ip_prefixes", "100.64.0.0/10").strip() or "100.64.0.0/10"

        # Create config directory
        headscale_dir = config.data_dir / "headscale"
        headscale_dir.mkdir(parents=True, exist_ok=True)

        # Write minimal config
        config_file = headscale_dir / "config.yaml"
        if not config_file.exists():
            config_file.write_text(
                f"""server_url: https://{domain}
listen_addr: 0.0.0.0:8080
metrics_listen_addr: 127.0.0.1:9090
grpc_listen_addr: 0.0.0.0:50443
grpc_allow_insecure: false

noise:
  private_key_path: /etc/headscale/noise_private.key

prefixes:
  v4: {ip_prefixes}
  allocation: sequential

derp:
  server:
    enabled: false
  urls:
    - https://controlplane.tailscale.com/derpmap/default
  auto_update_enabled: true
  update_frequency: 24h

disable_check_updates: false
ephemeral_node_inactivity_timeout: 30m

database:
  type: sqlite3
  sqlite:
    path: /var/lib/headscale/db.sqlite

log:
  format: text
  level: info

acls_policy_path: ""
dns_config:
  nameservers:
    - 1.1.1.1
  domains: []
  magic_dns: true
  base_domain: headscale.net
""",
                encoding="utf-8",
            )

        fragment = {
            "image": "headscale/headscale:latest",  # last-verified: 2026-06-21 — upstream-tracking float (#1228)
            "container_name": CONTAINER_NAME,
            "command": "headscale serve",
            "volumes": [
                f"{headscale_dir}:/etc/headscale",
                f"{headscale_dir}/data:/var/lib/headscale",
            ],
            "ports": [
                "8085:8080",  # Headscale API (different from SLOP port 8080)
                "9090:9090",  # Metrics
            ],
            "labels": {
                "traefik.enable": "true",
                "traefik.http.routers.headscale.rule": f"Host(`{domain}`)",
                "traefik.http.routers.headscale.entrypoints": "websecure",
                "traefik.http.routers.headscale.tls.certresolver": cert_resolver,
                "traefik.http.services.headscale.loadbalancer.server.port": "8080",
            },
            "restart": "unless-stopped",
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=60)
            if rc != 0:
                return ProviderResult.failure("Headscale failed to start.", _out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy Headscale.", str(e))

        with StateDB() as db:
            db.upsert_tunnel_provider(
                self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
            )

        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "headscale",
                    display_name=self.display_name,
                    tier=0,  # tier 0 = infrastructure layer
                    category=self.category,
                    status="running",
                    image="headscale/headscale:latest",
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=8080,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Headscale deployed at {domain}. "
            "Create a user: docker exec headscale headscale users create admin"
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                compose_down(frag_path)
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove Headscale.", str(e))
        with StateDB() as db:
            db.remove_tunnel_provider(self.key)
        return ProviderResult.success("Headscale removed.")

    def verify(self) -> ProviderResult:
        import subprocess

        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=5,
            )
            status = r.stdout.strip()
            return (
                ProviderResult.success("Headscale is running.")
                if status == "running"
                else ProviderResult.failure(f"Headscale status: {status or 'not found'}.", "")
            )
        except Exception as e:
            return ProviderResult.failure("Cannot check Headscale.", str(e))
