"""backend/infra/providers/management_komodo.py

Komodo management slot provider — moghtech/komodo (multi-server, Git-driven).

Extracted from management_alternatives.py (#987) to keep that file under its
line-size baseline. Shares the management-slot base + the managed-app
registration helper, both imported from management_alternatives.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import yaml

from backend.core import docker_client
from backend.core.compose import STACK_NETWORK, compose_up
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import ProviderResult
from backend.infra.providers.management_alternatives import (
    _ManagementProvider,
    _register_management_app,
)
from backend.infra.registry import register


@register
class KomodoProvider(_ManagementProvider):
    slot = "management"
    key = "komodo"
    display_name = "Komodo"
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": False,
            "help": "Base domain — Komodo is published at komodo.<domain>.",
        },
        {
            "key": "port",
            "label": "Host port",
            "type": "number",
            "placeholder": "9120",
            "required": False,
            "help": "Host port for the Komodo Core UI (default 9120).",
        },
        {
            "key": "jwt_secret",
            "label": "JWT secret",
            "type": "text",
            "secret": True,
            "required": False,
            "help": "Random string (min 32 chars) for JWT signing (defaults to ${KOMODO_JWT_SECRET}).",
        },
        {
            "key": "passkey",
            "label": "Periphery passkey",
            "type": "text",
            "secret": True,
            "required": False,
            "help": "Shared passkey for Core↔Periphery auth (defaults to ${KOMODO_PASSKEY}).",
        },
        {
            "key": "db_password",
            "label": "MongoDB password",
            "type": "text",
            "secret": True,
            "required": False,
            "help": "Password for MongoDB root user and Komodo DB connection (defaults to ${KOMODO_DB_PASSWORD}).",
        },
    ]

    # Single compose file that groups all three Komodo services.  Using a
    # combined file rather than three separate fragments is deliberate:
    # Docker Compose attaches every service to the external network in one
    # atomic operation, which eliminates the race / ordering issue that caused
    # Traefik to log "unable to find the IP address for container /komodo"
    # when the containers were started via independent docker compose invocations.
    _STACK_FILE = "komodo-stack.yaml"

    def _write_stack(
        self,
        network: str,
        backups_path: str,
        periphery_root: str,
        mongo_data: str,
        host_port: int,
        domain: str,
        jwt_secret: str,
        passkey: str,
        db_password: str,
        timezone: str,
    ) -> Path:
        """Write a single Compose file containing all three Komodo services.

        All three containers (core, periphery, mongo) share a single
        top-level ``networks:`` block so that every container is guaranteed
        to join the *network* network when the file is processed.  The
        previous approach of three separate fragment files used independent
        ``docker compose -f ...`` invocations; Docker's network-attach step
        on each invocation could fail silently, leaving a container on the
        default bridge instead of *network* — the root cause of #747.

        DB backend = MongoDB 7 (#1142): this provider previously rendered
        ``ghcr.io/ferretdb/ferretdb:latest`` pointed at the platform's plain
        Postgres, but ``:latest`` now resolves to FerretDB v2, which requires
        a DocumentDB-extended Postgres (moghtech/komodo#382) — not the plain
        ``postgres:5432`` — so the rendered stack was broken at the DB layer.
        MongoDB is upstream Komodo's recommended backend and matches the
        catalog manifest (``catalog/apps/komodo.yaml``), converging the two
        definitions — including root auth: the mongo root user and Komodo's DB
        connection both reference the same ``${KOMODO_DB_PASSWORD}`` (``komodo``
        / ``db_password``), so they always agree even when the secret is
        unpopulated. Data persists to a host volume (ferretdb persisted in
        platform Postgres; mongo needs its own ``/data/db`` volume or the DB is
        ephemeral across recreations).
        """
        mongo_svc: dict[str, Any] = {
            "image": "mongo:7",
            "container_name": "komodo-mongo",
            "restart": "unless-stopped",
            "networks": [network],
            "volumes": [f"{mongo_data}:/data/db"],
            "environment": {
                "MONGO_INITDB_ROOT_USERNAME": "komodo",
                "MONGO_INITDB_ROOT_PASSWORD": db_password,
            },
        }

        periphery_svc: dict[str, Any] = {
            "image": "ghcr.io/moghtech/komodo-periphery:latest",  # last-verified: 2026-06-21 — upstream-tracking float (#1228)
            "container_name": "komodo-periphery",
            "restart": "unless-stopped",
            "networks": [network],
            "volumes": [
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{periphery_root}:/etc/komodo",
            ],
            "environment": {
                "PERIPHERY_ROOT_DIRECTORY": "/etc/komodo",  # container-internal
                "PERIPHERY_PASSKEY": passkey,
            },
        }

        core_svc: dict[str, Any] = {
            "image": "ghcr.io/moghtech/komodo-core:latest",  # last-verified: 2026-06-21 — upstream-tracking float (#1228)
            "container_name": "komodo",
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{host_port}:9120"],
            "volumes": [f"{backups_path}:/backups"],
            "depends_on": ["komodo-mongo", "komodo-periphery"],
            "environment": {
                "TZ": timezone,
                "KOMODO_HOST": f"https://komodo.{domain}"
                if domain
                else f"http://localhost:{host_port}",
                "KOMODO_TITLE": "Komodo",
                "KOMODO_FIRST_SERVER": "https://komodo-periphery:8120",
                "KOMODO_DATABASE_ADDRESS": "komodo-mongo:27017",
                "KOMODO_DATABASE_USERNAME": "komodo",
                "KOMODO_DATABASE_PASSWORD": db_password,
                "KOMODO_JWT_SECRET": jwt_secret,
                "KOMODO_PASSKEY": passkey,
            },
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.komodo.rule=Host(`komodo.{domain}`)",
                "traefik.http.routers.komodo.entrypoints=websecure",
                "traefik.http.routers.komodo.tls=true",
                "traefik.http.services.komodo.loadbalancer.server.port=9120",
            ]
            if domain
            else [],
        }

        compose_dir = config.compose_dir
        compose_dir.mkdir(parents=True, exist_ok=True)

        content: dict[str, Any] = {
            "services": {
                "komodo-mongo": mongo_svc,
                "komodo-periphery": periphery_svc,
                "komodo": core_svc,
            },
            # Single top-level networks block so Docker Compose attaches ALL
            # three services to the external slop network in one operation.
            "networks": {network: {"external": True}},
        }

        stack_path = compose_dir / self._STACK_FILE
        stack_path.write_text(yaml.dump(content, default_flow_style=False, sort_keys=False))
        return stack_path

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy Komodo Core + Periphery agent + MongoDB.

        Required config:
          jwt_secret — random secret string (min 32 chars)
          passkey    — random passkey for Core↔Periphery auth
        Optional:
          port — host port override (default 9120)
          domain — base domain
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or STACK_NETWORK
        domain = cfg.get("domain") or platform.domain or ""
        host_port = cfg.get("port", 9120)
        jwt_secret = cfg.get("jwt_secret", "${KOMODO_JWT_SECRET}")
        passkey = cfg.get("passkey", "${KOMODO_PASSKEY}")
        db_password = cfg.get("db_password", "${KOMODO_DB_PASSWORD}")

        backups_path = str(config.data_dir / "komodo" / "backups")
        os.makedirs(backups_path, exist_ok=True)
        # Periphery's host-side root — bind-mounted into the container
        # at /etc/komodo (the periphery image expects that path inside).
        # Was hardcoded to /etc/komodo on the host, which required root
        # to create. Use config.data_dir/komodo/periphery instead.
        periphery_root = str(config.data_dir / "komodo" / "periphery")
        os.makedirs(periphery_root, exist_ok=True)
        # MongoDB data dir — mongo persists to /data/db, so it needs its own
        # host volume (#1142; the prior ferretdb path persisted in platform
        # Postgres). Without this the DB is wiped on container recreation.
        mongo_data = str(config.data_dir / "komodo" / "mongodb")
        os.makedirs(mongo_data, exist_ok=True)

        try:
            stack_path = self._write_stack(
                network=network,
                backups_path=backups_path,
                periphery_root=periphery_root,
                mongo_data=mongo_data,
                host_port=host_port,
                domain=domain,
                jwt_secret=jwt_secret,
                passkey=passkey,
                db_password=db_password,
                timezone=platform.timezone or "UTC",
            )
            rc, _out = compose_up(stack_path, timeout=120)
            if rc != 0:
                return ProviderResult.failure("Komodo failed to start.", detail=_out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Komodo: {e}")

        with StateDB() as db:
            db.update_slot(
                "management",
                status="active",
                provider="komodo",
                config={"port": host_port, "domain": domain},
            )

        url = f"https://komodo.{domain}" if domain else f"http://localhost:{host_port}"
        _register_management_app(
            self.key, self.display_name, "ghcr.io/moghtech/komodo-core:latest", "komodo", host_port
        )
        return ProviderResult.success(
            f"Komodo deployed at {url}. "
            f"Click 'Sign Up' (not 'Log In') to create the initial admin account. "
            f"The local server will be auto-registered via the Periphery agent."
        )

    def remove(self) -> ProviderResult:
        try:
            stack_path = config.compose_dir / self._STACK_FILE
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(stack_path),
                    "--env-file",
                    str(config.env_file),
                    "down",
                ],
                capture_output=True,
                timeout=60,
            )
            if stack_path.exists():
                stack_path.unlink()
        except Exception:  # noqa: S110  # best-effort teardown
            pass
        # Also clean up any legacy per-service fragment files left by older
        # installs that used three separate docker compose invocations.
        for legacy_key in ("komodo", "komodo-periphery", "komodo-mongo", "komodo-ferretdb"):
            legacy_path = config.compose_dir / f"{legacy_key}.yaml"
            if legacy_path.exists():
                try:
                    legacy_path.unlink()
                except OSError:  # best-effort removal
                    pass
        with StateDB() as db:
            db.update_slot("management", status="empty", provider=None, config={})
            # #1123: remove the apps-table row deploy() registered, else a stale
            # row lingers after a management-slot swap (cascade delete; no-op if absent).
            db.remove_app(self.key)
        return ProviderResult.success("Komodo and companions removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("komodo")
        if not c or c.status != "running":
            return ProviderResult.failure("Komodo Core is not running.")
        periphery = docker_client.get_container("komodo-periphery")
        if not periphery or periphery.status != "running":
            return ProviderResult.failure(
                "Komodo Core is running but Periphery agent is not. "
                "Containers cannot be managed without the agent."
            )
        return ProviderResult.success("Komodo Core and Periphery agent are running.")
