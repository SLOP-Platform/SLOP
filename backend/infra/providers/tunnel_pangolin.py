"""backend/infra/providers/tunnel_pangolin.py

Pangolin tunnel provider.

Runs the Pangolin client against an existing control plane using an enrollment
token. Like the other mesh-style tunnels, app-level hostnames stay with Traefik.
"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "pangolin"
IMAGE = "fosrl/pangolin-client:latest"


@register
class PangolinProvider(InfraProvider):
    slot = "tunnel"
    key = "pangolin"
    display_name = "Pangolin"
    category = "networking"
    description = "Reverse tunnel client for the Pangolin remote access platform."

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "enrollment_token",
            "label": "Enrollment token",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Client enrollment token from your Pangolin control plane.",
        },
        {
            "key": "controller_url",
            "label": "Controller URL",
            "type": "text",
            "placeholder": "https://pangolin.example.com",
            "required": True,
            "help": "Base URL of your Pangolin control plane.",
        },
        {
            "key": "hostname",
            "label": "Client hostname",
            "type": "text",
            "placeholder": "slop",
            "required": False,
            "help": "Optional hostname label to show in Pangolin.",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        enrollment_token = str(cfg.get("enrollment_token", "")).strip()
        controller_url = str(cfg.get("controller_url", "")).strip()
        if not enrollment_token or not controller_url:
            return ProviderResult.failure(
                "Pangolin controller URL and enrollment token are required."
            )
        hostname = str(cfg.get("hostname", "")).strip() or "slop"

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "network_mode": "host",
            "environment": {
                "PANGOLIN_CONTROLLER_URL": controller_url,
                "PANGOLIN_ENROLLMENT_TOKEN": enrollment_token,
                "PANGOLIN_HOSTNAME": hostname,
            },
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Pangolin failed to start.", out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy Pangolin.", str(e))

        with StateDB() as db:
            db.upsert_tunnel_provider(
                self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
                config={"controller_url": controller_url, "hostname": hostname},
            )
        return ProviderResult.success("Pangolin deployed and connected to its control plane.")

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                compose_down(frag_path)
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove Pangolin.", str(e))
        with StateDB() as db:
            db.remove_tunnel_provider(self.key)
        return ProviderResult.success("Pangolin removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("Pangolin container is not running.")
        return ProviderResult.success("Pangolin is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success(
            f"Pangolin provides host connectivity; Traefik continues routing {hostname} -> {target}."
        )

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Pangolin hostname management is handled by Traefik.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("Pangolin does not maintain per-app hostnames.", data={})

    def pre_migration_snapshot(self) -> ProviderResult:
        return ProviderResult.success("Pangolin tunnel state is external; no local snapshot required.", data={})
