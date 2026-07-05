"""backend/infra/providers/management_alternatives.py

Alternative container management providers:
  DockhandProvider   — fnsys/dockhand (recommended Portainer CE replacement)
  DockgeProvider     — louislam/dockge (Compose-first stack manager)

Also home to the shared management-slot base (``_ManagementProvider``) and the
managed-app registration helper (``_register_management_app``), which the two
extracted providers import:
  KomodoProvider      -> management_komodo.py
  PortainerBEProvider -> management_portainer_be.py
(extracted in #987 to keep this file under its line-size baseline.)
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, ClassVar


from backend.core import docker_client
from backend.core.compose import compose_up, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register


def _register_management_app(
    key: str, display_name: str, image: str, container_name: str, host_port: int
) -> None:
    """Register an infra management app as a fully managed app (health-monitored,
    Dashboard-visible, with operation history) — identical to a catalog install and
    to the other infra providers (dashboard_homepage/glance, tunnel_*). Best-effort:
    a DB hiccup must not fail an otherwise-good deploy. Called from deploy() — NOT
    from the read-only list_hostnames, where this upsert used to be smuggled (#994).
    """
    try:
        import time as _t

        with StateDB() as _db:
            _db.upsert_app(
                key,
                display_name=display_name,
                tier=0,
                category="management",
                status="running",
                image=image,
                image_tag="latest",
                container_name=container_name,
                host_port=host_port,
                last_healthy_at=int(_t.time()),
            )
    except Exception as _e:
        import logging

        logging.getLogger(__name__).debug("Could not register infra app in DB: %s", _e)


class _ManagementProvider(InfraProvider):
    """Shared base for container-management slot providers.

    They expose no public hostnames (the managed app is reached directly, not
    via the tunnel), so the three tunnel-interface methods are identical no-ops
    across every management provider — defined once here (SSOT) instead of
    copied into each class.
    """

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        # Pure read — no side-effects. Registration happens in deploy() (#994).
        return ProviderResult.success("No external hostnames.", data={})


# ---------------------------------------------------------------------------
# Dockhand
# ---------------------------------------------------------------------------


@register
class DockhandProvider(_ManagementProvider):
    slot = "management"
    key = "dockhand"
    display_name = "Dockhand"
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": False,
            "help": "Base domain — Dockhand is published at dockhand.<domain>.",
        },
        {
            "key": "port",
            "label": "Host port",
            "type": "number",
            "placeholder": "3000",
            "required": False,
            "help": "Host port to publish the UI on (default 3000).",
        },
        {
            "key": "use_postgres",
            "label": "Use PostgreSQL",
            "type": "checkbox",
            "required": False,
            "help": "Use shared PostgreSQL backend instead of SQLite (default).",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy Dockhand — zero telemetry, SQLite or PostgreSQL.

        Optional config:
          port — host port override (default 3000)
          use_postgres — bool, use shared PostgreSQL instead of SQLite
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", 3000)
        use_postgres = cfg.get("use_postgres", False)

        data_path = str(config.data_dir / "dockhand")
        os.makedirs(data_path, exist_ok=True)

        env: dict[str, str] = {"TZ": platform.timezone or "UTC"}
        if use_postgres:
            env["DATABASE_URL"] = (
                "postgresql://dockhand:${DOCKHAND_DB_PASSWORD}@postgres:5432/dockhand"
            )

        fragment = {
            "image": "fnsys/dockhand:latest",  # last-verified: 2026-06-21 — upstream-tracking float (#1228)
            "container_name": "dockhand",
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{host_port}:3000"],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{data_path}:/app/data",
            ],
            "environment": env,
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.dockhand.rule=Host(`dockhand.{domain}`)",
                "traefik.http.routers.dockhand.entrypoints=websecure",
                "traefik.http.routers.dockhand.tls=true",
                "traefik.http.services.dockhand.loadbalancer.server.port=3000",
            ]
            if domain
            else [],
        }

        try:
            frag_path = write_fragment("dockhand", fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Dockhand failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Dockhand: {e}")

        with StateDB() as db:
            db.update_slot(
                "management",
                status="active",
                provider="dockhand",
                config={"port": host_port, "domain": domain},
            )

        url = f"https://dockhand.{domain}" if domain else f"http://localhost:{host_port}"
        _register_management_app(
            self.key, self.display_name, "fnsys/dockhand:latest", "dockhand", host_port
        )
        return ProviderResult.success(
            f"Dockhand deployed at {url}. "
            f"Zero telemetry, SQLite storage. Create your admin account on first login."
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / "dockhand.yaml"
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
            return ProviderResult.failure(f"Could not remove Dockhand: {e}")
        with StateDB() as db:
            db.update_slot("management", status="empty", provider=None, config={})
            # #1123: remove the apps-table row deploy() registered, else a stale
            # row lingers after a management-slot swap (cascade delete; no-op if absent).
            db.remove_app(self.key)
        return ProviderResult.success("Dockhand removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("dockhand")
        if not c or c.status != "running":
            return ProviderResult.failure("Dockhand is not running.")
        return ProviderResult.success("Dockhand is running.")


# ---------------------------------------------------------------------------
# Dockge
# ---------------------------------------------------------------------------


@register
class DockgeProvider(_ManagementProvider):
    slot = "management"
    key = "dockge"
    display_name = "Dockge"
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": False,
            "help": "Base domain — Dockge is published at dockge.<domain>.",
        },
        {
            "key": "port",
            "label": "Host port",
            "type": "number",
            "placeholder": "5001",
            "required": False,
            "help": "Host port to publish the UI on (default 5001).",
        },
        {
            "key": "stacks_dir",
            "label": "Stacks directory",
            "type": "text",
            "required": False,
            "help": "Host path for Compose stacks — must match container-side path (default: data_dir/dockge/stacks).",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy Dockge — Compose-first stack manager.

        Optional config:
          port — host port override (default 5001)
          stacks_dir — host path for compose stacks (default
                       config.data_dir / 'dockge' / 'stacks').
                       MUST be same path inside and outside the container.
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", 5001)
        # Default to a path under config.data_dir — was hardcoded
        # `/opt/stacks` which required root privileges to create on
        # the host. Advanced users can still override via cfg.
        stacks_dir = cfg.get("stacks_dir", str(config.data_dir / "dockge" / "stacks"))

        # Create both data and stacks directories
        data_path = str(config.data_dir / "dockge")
        os.makedirs(data_path, exist_ok=True)
        os.makedirs(stacks_dir, exist_ok=True)

        fragment = {
            "image": "louislam/dockge:1",
            "container_name": "dockge",
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{host_port}:5001"],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{data_path}:/app/data",
                f"{stacks_dir}:{stacks_dir}",  # same path inside and outside
            ],
            "environment": {
                "TZ": platform.timezone or "UTC",
                "DOCKGE_STACKS_DIR": stacks_dir,
            },
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.dockge.rule=Host(`dockge.{domain}`)",
                "traefik.http.routers.dockge.entrypoints=websecure",
                "traefik.http.routers.dockge.tls=true",
                "traefik.http.services.dockge.loadbalancer.server.port=5001",
            ]
            if domain
            else [],
        }

        try:
            frag_path = write_fragment("dockge", fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Dockge failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Dockge: {e}")

        with StateDB() as db:
            db.update_slot(
                "management",
                status="active",
                provider="dockge",
                config={"port": host_port, "stacks_dir": stacks_dir},
            )

        url = f"https://dockge.{domain}" if domain else f"http://localhost:{host_port}"
        _register_management_app(
            self.key, self.display_name, "louislam/dockge:1", "dockge", host_port
        )
        return ProviderResult.success(
            f"Dockge deployed at {url}. "
            f"Stacks directory: {stacks_dir}. "
            f"Note: only manage containers via Compose stacks — Dockge has no single-container view."
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / "dockge.yaml"
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
            return ProviderResult.failure(f"Could not remove Dockge: {e}")
        with StateDB() as db:
            db.update_slot("management", status="empty", provider=None, config={})
            # #1123: remove the apps-table row deploy() registered, else a stale
            # row lingers after a management-slot swap (cascade delete; no-op if absent).
            db.remove_app(self.key)
        return ProviderResult.success("Dockge removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("dockge")
        if not c or c.status != "running":
            return ProviderResult.failure("Dockge is not running.")
        return ProviderResult.success("Dockge is running.")
