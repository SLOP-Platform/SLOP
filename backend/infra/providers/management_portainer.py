"""backend/infra/providers/management_portainer.py

Portainer container management provider.

Deploys Portainer CE — the visual Docker management UI.
"""

from __future__ import annotations

import subprocess
from typing import Any

from backend.core import docker_client
from backend.core.compose import compose_up, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "portainer"
IMAGE = "portainer/portainer-ce:latest"
INTERNAL_PORT = 9000
EDGE_PORT = 8000


@register
class PortainerProvider(InfraProvider):
    slot = "management"
    key = "portainer"
    display_name = "Portainer CE"

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", INTERNAL_PORT)

        data_path = str(config.data_dir / "portainer")

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [
                f"{host_port}:{INTERNAL_PORT}",
                f"{EDGE_PORT}:{EDGE_PORT}",
            ],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock:ro",
                f"{data_path}:/data",
            ],
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.portainer.rule=Host(`portainer.{domain}`)",
                "traefik.http.routers.portainer.entrypoints=websecure",
                "traefik.http.routers.portainer.tls=true",
                f"traefik.http.services.portainer.loadbalancer.server.port={INTERNAL_PORT}",
            ]
            if domain
            else [],
        }

        try:
            import os

            os.makedirs(data_path, exist_ok=True)
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Portainer failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Portainer: {e}")

        with StateDB() as db:
            db.update_slot(
                "management",
                status="active",
                provider="portainer",
                config={"port": host_port, "domain": domain},
            )

        url = f"https://portainer.{domain}" if domain else f"http://localhost:{host_port}"
        return ProviderResult.success(
            f"Portainer CE deployed at {url}. Create your admin account on first login."
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(frag_path),
                    "--env-file",
                    str(config.env_file),
                    "down",
                ],
                capture_output=True,
                timeout=30,
            )
            if frag_path.exists():
                frag_path.unlink()
        except Exception as e:
            return ProviderResult.failure(f"Could not remove Portainer: {e}")
        with StateDB() as db:
            db.update_slot("management", status="empty", provider=None, config={})
        return ProviderResult.success("Portainer removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("Portainer is not running.")
        return ProviderResult.success("Portainer is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        try:
            from backend.core.state import StateDB as _SDB_p
            import time as _t_p

            with _SDB_p() as _db_p:
                _db_p.upsert_app(
                    "portainer",
                    display_name="Portainer",
                    tier=0,
                    category="management",
                    status="running",
                    image=IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=9000,
                    last_healthy_at=int(_t_p.time()),
                )
        except Exception:  # noqa: S110  # best-effort DB update; provider result returned regardless
            pass

        return ProviderResult.success("Portainer has no external hostnames.", data={})
