"""backend/infra/providers/tunnel_zerotier.py

ZeroTier tunnel provider.

Joins the host to a ZeroTier network using an operator-provided network id.
"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "zerotier"
IMAGE = "zerotier/zerotier:latest"


@register
class ZeroTierProvider(InfraProvider):
    slot = "tunnel"
    key = "zerotier"
    display_name = "ZeroTier"
    category = "networking"
    description = "Private mesh networking using a ZeroTier controller and network ID."

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "network_id",
            "label": "Network ID",
            "type": "text",
            "required": True,
            "help": "16-digit ZeroTier network id to join.",
        },
        {
            "key": "identity_public",
            "label": "Identity public (optional)",
            "type": "text",
            "required": False,
            "secret": True,
            "help": "Optional persisted identity.public contents.",
        },
        {
            "key": "identity_secret",
            "label": "Identity secret (optional)",
            "type": "text",
            "required": False,
            "secret": True,
            "help": "Optional persisted identity.secret contents.",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        network_id = str(cfg.get("network_id", "")).strip()
        if not network_id:
            return ProviderResult.failure("ZeroTier network ID is required.")

        env: dict[str, str] = {"ZEROTIER_NETWORK_ID": network_id}
        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "network_mode": "host",
            "cap_add": ["NET_ADMIN", "SYS_ADMIN"],
            "devices": ["/dev/net/tun:/dev/net/tun"],
            "volumes": [f"{config.data_dir}/zerotier:/var/lib/zerotier-one"],
            "command": f"{network_id}",
            "environment": env,
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("ZeroTier failed to start.", out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy ZeroTier.", str(e))

        with StateDB() as db:
            db.upsert_tunnel_provider(
                self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
                config={"network_id": network_id},
            )
        return ProviderResult.success("ZeroTier deployed and joined to the requested network.")

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                compose_down(frag_path)
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove ZeroTier.", str(e))
        with StateDB() as db:
            db.remove_tunnel_provider(self.key)
        return ProviderResult.success("ZeroTier removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("ZeroTier container is not running.")
        return ProviderResult.success("ZeroTier is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success(
            f"ZeroTier exposes the host network-wide; Traefik continues routing {hostname} -> {target}."
        )

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("ZeroTier hostname management is handled by Traefik.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("ZeroTier does not maintain per-app hostnames.", data={})

    def pre_migration_snapshot(self) -> ProviderResult:
        return ProviderResult.success("ZeroTier tunnel state is external; no local snapshot required.", data={})
