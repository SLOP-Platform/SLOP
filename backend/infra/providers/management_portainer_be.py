"""backend/infra/providers/management_portainer_be.py

Portainer Business Edition management slot provider (portainer/portainer-ee).

Extracted from management_alternatives.py (#987) to keep that file under its
line-size baseline. Shares the management-slot base + the managed-app
registration helper, both imported from management_alternatives.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import ProviderResult
from backend.infra.providers.management_alternatives import (
    _ManagementProvider,
    _register_management_app,
)
from backend.infra.registry import register


@register
class PortainerBEProvider(_ManagementProvider):
    slot = "management"
    key = "portainer_be"
    display_name = "Portainer Business Edition"
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": False,
            "help": "Base domain — Portainer BE is published at portainer.<domain>.",
        },
        {
            "key": "port",
            "label": "Host port",
            "type": "number",
            "placeholder": "9000",
            "required": False,
            "help": "Host port to publish the UI on (default 9000).",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy Portainer Business Edition.

        Required config:
          (none at deploy time — license is uploaded through the Portainer UI)

        The BE license is NOT passed as an environment variable.
        After deploying, go to Settings → Licenses in the Portainer UI
        and paste your license key there.

        Portainer BE adds over CE:
          - RBAC with granular permissions
          - Registry management
          - GitOps (Git-driven stack deployments)
          - Support (SLA-backed)
          - More: https://www.portainer.io/pricing
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", 9000)

        data_path = str(config.data_dir / "portainer-be")
        os.makedirs(data_path, exist_ok=True)

        fragment = {
            "image": "portainer/portainer-ee:latest",  # EE = Business Edition; last-verified: 2026-06-21 (#1228)
            "container_name": "portainer",
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [
                f"{host_port}:9000",
                "8000:8000",
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
                "traefik.http.services.portainer.loadbalancer.server.port=9000",
            ]
            if domain
            else [],
        }

        try:
            frag_path = write_fragment("portainer", fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Portainer BE failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Portainer BE: {e}")

        with StateDB() as db:
            db.update_slot(
                "management",
                status="active",
                provider="portainer_be",
                config={"port": host_port, "edition": "BE"},
            )

        url = f"https://portainer.{domain}" if domain else f"http://localhost:{host_port}"
        # PortainerBE registered nowhere before #994 (no smuggled list_hostnames
        # upsert either); register at deploy for parity with the family.
        _register_management_app(
            self.key, self.display_name, "portainer/portainer-ee:latest", "portainer", host_port
        )
        return ProviderResult.success(
            f"Portainer Business Edition deployed at {url}. "
            f"Create your admin account on first login, then go to "
            f"Settings → Licenses to activate your Business Edition license key."
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / "portainer.yaml"
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
            return ProviderResult.failure(f"Could not remove Portainer BE: {e}")
        with StateDB() as db:
            db.update_slot("management", status="empty", provider=None, config={})
            # #1123: remove the apps-table row deploy() registered, else a stale
            # row lingers after a management-slot swap (cascade delete; no-op if absent).
            db.remove_app(self.key)
        return ProviderResult.success("Portainer Business Edition removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("portainer")
        if not c or c.status != "running":
            return ProviderResult.failure("Portainer BE is not running.")
        return ProviderResult.success("Portainer Business Edition is running.")
