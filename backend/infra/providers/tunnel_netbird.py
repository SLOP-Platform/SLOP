"""backend/infra/providers/tunnel_netbird.py

NetBird tunnel provider.

Joins the host to an existing NetBird mesh using a setup key. Per-app
hostnames are still routed by Traefik, so the tunnel contract verbs are
best-effort/no-op like Tailscale's.
"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "netbird"
IMAGE = "netbirdio/netbird:latest"


@register
class NetBirdProvider(InfraProvider):
    slot = "tunnel"
    key = "netbird"
    display_name = "NetBird"
    category = "networking"
    description = "Private mesh networking with a managed or self-hosted NetBird control plane."

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "setup_key",
            "label": "Setup key",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Reusable setup key from your NetBird admin panel.",
        },
        {
            "key": "management_url",
            "label": "Management URL",
            "type": "text",
            "placeholder": "https://api.netbird.io",
            "required": False,
            "help": "Optional override for a self-hosted NetBird management endpoint.",
        },
        {
            "key": "admin_url",
            "label": "Admin URL",
            "type": "text",
            "placeholder": "https://app.netbird.io",
            "required": False,
            "help": "Optional override for a self-hosted NetBird admin URL.",
        },
        {
            "key": "hostname",
            "label": "Mesh hostname",
            "type": "text",
            "placeholder": "slop",
            "required": False,
            "help": "How this node appears inside your NetBird network.",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        setup_key = str(cfg.get("setup_key", "")).strip()
        if not setup_key:
            return ProviderResult.failure("NetBird setup key is required.")

        hostname = str(cfg.get("hostname", "")).strip() or "slop"
        env: dict[str, str] = {
            "NB_SETUP_KEY": setup_key,
            "NB_HOSTNAME": hostname,
        }
        management_url = str(cfg.get("management_url", "")).strip()
        admin_url = str(cfg.get("admin_url", "")).strip()
        if management_url:
            env["NB_MANAGEMENT_URL"] = management_url
        if admin_url:
            env["NB_ADMIN_URL"] = admin_url

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "network_mode": "host",
            "cap_add": ["NET_ADMIN", "SYS_ADMIN", "SYS_RESOURCE"],
            "devices": ["/dev/net/tun:/dev/net/tun"],
            "volumes": [
                f"{config.data_dir}/netbird:/etc/netbird",
            ],
            "environment": env,
            "command": "netbird up --foreground --setup-key ${NB_SETUP_KEY}",
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("NetBird failed to start.", out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy NetBird.", str(e))

        with StateDB() as db:
            db.upsert_tunnel_provider(
                self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
                config={"hostname": hostname},
            )

        return ProviderResult.success("NetBird deployed and joined to your mesh network.")

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                compose_down(frag_path)
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove NetBird.", str(e))
        with StateDB() as db:
            db.remove_tunnel_provider(self.key)
        return ProviderResult.success("NetBird removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("NetBird container is not running.")
        return ProviderResult.success("NetBird is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success(
            f"NetBird exposes the host mesh-wide; Traefik continues routing {hostname} -> {target}."
        )

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("NetBird hostname management is handled by Traefik.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("NetBird does not maintain per-app hostnames.", data={})

    def pre_migration_snapshot(self) -> ProviderResult:
        return ProviderResult.success(
            "NetBird tunnel state is external; no local snapshot required.", data={}
        )
