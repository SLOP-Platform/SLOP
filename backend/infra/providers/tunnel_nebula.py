"""backend/infra/providers/tunnel_nebula.py

Nebula tunnel provider.

Runs a Nebula node from operator-provided certs/config. SLOP does not author the
mesh config; it only wires the runtime container.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "nebula"
IMAGE = "slonopotamus/nebula:latest"


@register
class NebulaProvider(InfraProvider):
    slot = "tunnel"
    key = "nebula"
    display_name = "Nebula"
    category = "networking"
    description = "Private WireGuard-like overlay network using operator-managed Nebula certs."

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "config_yaml",
            "label": "nebula config.yml",
            "type": "textarea",
            "required": True,
            "secret": True,
            "help": "Paste the full Nebula config YAML for this node.",
        },
        {
            "key": "ca_crt",
            "label": "CA certificate",
            "type": "textarea",
            "required": True,
            "secret": True,
            "help": "Paste the Nebula CA certificate contents.",
        },
        {
            "key": "host_crt",
            "label": "Host certificate",
            "type": "textarea",
            "required": True,
            "secret": True,
            "help": "Paste this node's Nebula host certificate.",
        },
        {
            "key": "host_key",
            "label": "Host private key",
            "type": "textarea",
            "required": True,
            "secret": True,
            "help": "Paste this node's Nebula private key.",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        required = {
            "config_yaml": str(cfg.get("config_yaml", "")).strip(),
            "ca_crt": str(cfg.get("ca_crt", "")).strip(),
            "host_crt": str(cfg.get("host_crt", "")).strip(),
            "host_key": str(cfg.get("host_key", "")).strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            return ProviderResult.failure(
                "Nebula configuration is incomplete.",
                f"Missing: {', '.join(sorted(missing))}",
            )

        nebula_dir = Path(config.data_dir) / "nebula"
        nebula_dir.mkdir(parents=True, exist_ok=True)
        nebula_dir.chmod(0o700)
        for fname, content in [
            ("config.yml", required["config_yaml"]),
            ("ca.crt", required["ca_crt"]),
            ("host.crt", required["host_crt"]),
            ("host.key", required["host_key"]),
        ]:
            fpath = nebula_dir / fname
            fpath.write_text(content, encoding="utf-8")
            fpath.chmod(0o600)

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "network_mode": "host",
            "cap_add": ["NET_ADMIN"],
            "devices": ["/dev/net/tun:/dev/net/tun"],
            "volumes": [f"{nebula_dir}:/etc/nebula"],
            "command": "-config /etc/nebula/config.yml",
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Nebula failed to start.", out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy Nebula.", str(e))

        with StateDB() as db:
            db.upsert_tunnel_provider(
                self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
            )
        return ProviderResult.success("Nebula deployed using the supplied mesh configuration.")

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                compose_down(frag_path)
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove Nebula.", str(e))
        with StateDB() as db:
            db.remove_tunnel_provider(self.key)
        return ProviderResult.success("Nebula removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("Nebula container is not running.")
        return ProviderResult.success("Nebula is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success(
            f"Nebula provides mesh reachability for the host; Traefik continues routing {hostname} -> {target}."
        )

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Nebula hostname management is handled by Traefik.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("Nebula does not maintain per-app hostnames.", data={})

    def pre_migration_snapshot(self) -> ProviderResult:
        return ProviderResult.success(
            "Nebula tunnel state is external; no local snapshot required.", data={}
        )
