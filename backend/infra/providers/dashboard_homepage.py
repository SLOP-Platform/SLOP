"""backend/infra/providers/dashboard_homepage.py

Homepage dashboard provider.

Deploys Homepage — the modern, fast dashboard with built-in widget support
for Sonarr, Radarr, Plex, and many other self-hosted apps.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "homepage"
IMAGE = "ghcr.io/gethomepage/homepage:latest"  # last-verified: 2026-06-21 — upstream-tracking float (#1228)
INTERNAL_PORT = 3000


@register
class HomepageProvider(InfraProvider):
    slot = "dashboard"
    key = "homepage"
    display_name = "Homepage"

    # MANIFEST-LESS infra provider (no catalog/apps/homepage.yaml) — `fields` is
    # hand-authored from this provider's own deploy() config knowledge
    # (domain/port). Judgment-class gap: an infra slot provider with no catalog
    # manifest has no manifest SSOT for its UI schema (see #975 follow-up).
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Base domain — Homepage is published at homepage.<domain>.",
        },
        {
            "key": "port",
            "label": "Host port",
            "type": "number",
            "placeholder": "3000",
            "required": False,
            "help": "Host port to publish the UI on (container port is 3000).",
        },
    ]
    category = "dashboard"

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy Homepage dashboard.

        Optional config:
          port — host port override (default: 3000)
          domain — base domain for Traefik routing
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", INTERNAL_PORT)

        # Create config directories — Homepage reads YAML configs from here
        cfg_dir = (
            Path(cfg.get("config_root") or platform.config_root or config.data_dir) / "homepage"
        )
        for sub in ("config",):
            (cfg_dir / sub).mkdir(parents=True, exist_ok=True)

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{host_port}:{INTERNAL_PORT}"],
            "volumes": [
                f"{cfg_dir}/config:/app/config",
                "/var/run/docker.sock:/var/run/docker.sock:ro",
            ],
            "environment": {"TZ": platform.timezone or "UTC"},
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.homepage.rule=Host(`home.{domain}`)",
                "traefik.http.routers.homepage.entrypoints=websecure",
                "traefik.http.routers.homepage.tls=true",
                f"traefik.http.services.homepage.loadbalancer.server.port={INTERNAL_PORT}",
            ]
            if domain
            else [],
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Homepage failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Homepage: {e}")

        with StateDB() as db:
            db.update_slot(
                "dashboard",
                status="active",
                provider="homepage",
                config={"port": host_port, "domain": domain},
            )

        url = f"https://home.{domain}" if domain else f"http://localhost:{host_port}"
        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "homepage",
                    display_name=self.display_name,
                    tier=0,  # tier 0 = infrastructure layer
                    category=self.category,
                    status="running",
                    image=IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=3000,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Homepage deployed. Access at {url}. "
            f"Edit YAML files in {cfg_dir}/config/ to configure widgets."
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
            return ProviderResult.failure(f"Could not remove Homepage: {e}")
        with StateDB() as db:
            db.update_slot("dashboard", status="empty", provider=None, config={})
        return ProviderResult.success("Homepage removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("Homepage is not running.")
        return ProviderResult.success("Homepage is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Dashboard provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Dashboard provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("Dashboard has no external hostnames.", data={})
