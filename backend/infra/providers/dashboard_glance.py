"""backend/infra/providers/dashboard_glance.py

Glance dashboard provider.

Deploys Glance — the feed-centric dashboard with RSS, Reddit, YouTube,
weather, Docker stats, and 20+ other widget types.
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

CONTAINER_NAME = "glance"
IMAGE = "glanceapp/glance:latest"  # last-verified: 2026-06-21 — upstream-tracking float (#1228)
INTERNAL_PORT = 8080

# Minimal starter glance.yml — user customises after deploy
STARTER_CONFIG = """\
# Glance dashboard — managed by SLOP
# Full docs: https://github.com/glanceapp/glance/blob/main/docs/configuration.md

server:
  port: 8080

theme:
  background-color: 240 10 10
  primary-color: 30 100 60

pages:
  - name: Home
    columns:
      - size: full
        widgets:
          - type: group
            widgets:
              - type: clock
                hour-format: "12h"
              - type: weather
                location: "New York, USA"
          - type: monitor
            title: Docker Containers
            show-failing-only: false
"""


@register
class GlanceDashboardProvider(InfraProvider):
    slot = "dashboard"
    key = "glance"
    display_name = "Glance"

    # Derived from catalog/apps/glance.yaml via `add_app.py --as-provider --key glance`
    # (manifest is the SSOT — #975). UI schema only; behaviour-neutral.
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Base domain — Glance is published at glance.<domain>.",
        },
        {
            "key": "port",
            "label": "Host port",
            "type": "number",
            "placeholder": "8080",
            "required": False,
            "help": "Host port to publish the UI on (container port is 8080).",
        },
    ]
    category = "dashboard"

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", INTERNAL_PORT)

        # Write starter config if not already present
        cfg_dir = Path(cfg.get("config_root") or platform.config_root or config.data_dir) / "glance"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        glance_yml = cfg_dir / "glance.yml"
        if not glance_yml.exists():
            glance_yml.write_text(STARTER_CONFIG)

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{host_port}:{INTERNAL_PORT}"],
            "volumes": [
                # glance reads its config from /app/config/glance.yml (matches the
                # catalog glance.yaml SSOT volume `config: /app/config`). Mounting the
                # starter at /app/glance.yml left it unread → built-in default loaded
                # (#1140, #975 child).
                f"{glance_yml}:/app/config/glance.yml:ro",
                "/etc/timezone:/etc/timezone:ro",
                "/etc/localtime:/etc/localtime:ro",
                "/var/run/docker.sock:/var/run/docker.sock:ro",
            ],
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.glance.rule=Host(`glance.{domain}`)",
                "traefik.http.routers.glance.entrypoints=websecure",
                "traefik.http.routers.glance.tls=true",
                f"traefik.http.services.glance.loadbalancer.server.port={INTERNAL_PORT}",
            ]
            if domain
            else [],
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Glance failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Glance: {e}")

        with StateDB() as db:
            db.update_slot(
                "dashboard",
                status="active",
                provider="glance",
                config={"port": host_port, "config_path": str(glance_yml)},
            )

        url = f"https://glance.{domain}" if domain else f"http://localhost:{host_port}"
        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "glance",
                    display_name=self.display_name,
                    tier=0,  # tier 0 = infrastructure layer
                    category=self.category,
                    status="running",
                    image=IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=8080,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Glance deployed at {url}. "
            f"Edit {glance_yml} to customise widgets, then restart Glance."
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
            return ProviderResult.failure(f"Could not remove Glance: {e}")
        with StateDB() as db:
            db.update_slot("dashboard", status="empty", provider=None, config={})
        return ProviderResult.success("Glance removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("Glance is not running.")
        return ProviderResult.success("Glance is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Dashboard — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Dashboard — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("Glance has no external hostnames.", data={})
