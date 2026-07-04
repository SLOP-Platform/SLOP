"""backend/infra/providers/management_alternatives.py

Alternative container management providers:
  DockhandProvider   — fnsys/dockhand (recommended Portainer CE replacement)
  DockgeProvider     — louislam/dockge (Compose-first stack manager)
  KomodoProvider     — moghtech/komodo (multi-server, Git-driven)
  PortainerBEProvider — portainer/portainer-ee (Business Edition, license required)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from backend.core import docker_client
from backend.core.compose import STACK_NETWORK, compose_up, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register


# ---------------------------------------------------------------------------
# Dockhand
# ---------------------------------------------------------------------------


@register
class DockhandProvider(InfraProvider):
    slot = "management"
    key = "dockhand"
    display_name = "Dockhand"

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
            "image": "fnsys/dockhand:latest",
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
        return ProviderResult.success("Dockhand removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("dockhand")
        if not c or c.status != "running":
            return ProviderResult.failure("Dockhand is not running.")
        return ProviderResult.success("Dockhand is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        try:
            from backend.core.state import StateDB as _SDB_dockhand
            import time as _t_dockhand

            with _SDB_dockhand() as _db_dockhand:
                _db_dockhand.upsert_app(
                    "dockhand",
                    display_name="Dockhand",
                    tier=0,
                    category="management",
                    status="running",
                    image="fnsys/dockhand:latest",
                    image_tag="latest",
                    container_name="dockhand",
                    host_port=8080,
                    last_healthy_at=int(_t_dockhand.time()),
                )
        except Exception:  # noqa: S110  # best-effort DB update; provider result returned regardless
            pass

        return ProviderResult.success("No external hostnames.", data={})


# ---------------------------------------------------------------------------
# Dockge
# ---------------------------------------------------------------------------


@register
class DockgeProvider(InfraProvider):
    slot = "management"
    key = "dockge"
    display_name = "Dockge"

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
        return ProviderResult.success("Dockge removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("dockge")
        if not c or c.status != "running":
            return ProviderResult.failure("Dockge is not running.")
        return ProviderResult.success("Dockge is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        try:
            from backend.core.state import StateDB as _SDB_dockge
            import time as _t_dockge

            with _SDB_dockge() as _db_dockge:
                _db_dockge.upsert_app(
                    "dockge",
                    display_name="Dockge",
                    tier=0,
                    category="management",
                    status="running",
                    image="louislam/dockge:1",
                    image_tag="latest",
                    container_name="dockge",
                    host_port=5001,
                    last_healthy_at=int(_t_dockge.time()),
                )
        except Exception:  # noqa: S110  # best-effort DB update; provider result returned regardless
            pass

        return ProviderResult.success("No external hostnames.", data={})


# ---------------------------------------------------------------------------
# Komodo
# ---------------------------------------------------------------------------


@register
class KomodoProvider(InfraProvider):
    slot = "management"
    key = "komodo"
    display_name = "Komodo"

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
        host_port: int,
        domain: str,
        jwt_secret: str,
        passkey: str,
        timezone: str,
    ) -> Path:
        """Write a single Compose file containing all three Komodo services.

        All three containers (core, periphery, ferretdb) share a single
        top-level ``networks:`` block so that every container is guaranteed
        to join the *network* network when the file is processed.  The
        previous approach of three separate fragment files used independent
        ``docker compose -f ...`` invocations; Docker's network-attach step
        on each invocation could fail silently, leaving a container on the
        default bridge instead of *network* — the root cause of #747.
        """
        ferretdb_svc: dict[str, Any] = {
            "image": "ghcr.io/ferretdb/ferretdb:latest",
            "container_name": "komodo-ferretdb",
            "restart": "unless-stopped",
            "networks": [network],
            "environment": {
                "FERRETDB_POSTGRESQL_URL": "postgres://slop:${POSTGRES_PASSWORD}@postgres:5432/komodo",
            },
        }

        periphery_svc: dict[str, Any] = {
            "image": "ghcr.io/moghtech/komodo-periphery:latest",
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
            "image": "ghcr.io/moghtech/komodo-core:latest",
            "container_name": "komodo",
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{host_port}:9120"],
            "volumes": [f"{backups_path}:/backups"],
            "depends_on": ["komodo-ferretdb", "komodo-periphery"],
            "environment": {
                "TZ": timezone,
                "KOMODO_HOST": f"https://komodo.{domain}"
                if domain
                else f"http://localhost:{host_port}",
                "KOMODO_TITLE": "Komodo",
                "KOMODO_FIRST_SERVER": "https://komodo-periphery:8120",
                "KOMODO_DATABASE_ADDRESS": "komodo-ferretdb:27017",
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
                "komodo-ferretdb": ferretdb_svc,
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
        """Deploy Komodo Core + Periphery agent + FerretDB.

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

        backups_path = str(config.data_dir / "komodo" / "backups")
        os.makedirs(backups_path, exist_ok=True)
        # Periphery's host-side root — bind-mounted into the container
        # at /etc/komodo (the periphery image expects that path inside).
        # Was hardcoded to /etc/komodo on the host, which required root
        # to create. Use config.data_dir/komodo/periphery instead.
        periphery_root = str(config.data_dir / "komodo" / "periphery")
        os.makedirs(periphery_root, exist_ok=True)

        try:
            stack_path = self._write_stack(
                network=network,
                backups_path=backups_path,
                periphery_root=periphery_root,
                host_port=host_port,
                domain=domain,
                jwt_secret=jwt_secret,
                passkey=passkey,
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
        for legacy_key in ("komodo", "komodo-periphery", "komodo-ferretdb"):
            legacy_path = config.compose_dir / f"{legacy_key}.yaml"
            if legacy_path.exists():
                try:
                    legacy_path.unlink()
                except OSError:  # best-effort removal
                    pass
        with StateDB() as db:
            db.update_slot("management", status="empty", provider=None, config={})
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

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        try:
            from backend.core.state import StateDB as _SDB_komodo
            import time as _t_komodo

            with _SDB_komodo() as _db_komodo:
                _db_komodo.upsert_app(
                    "komodo",
                    display_name="Komodo",
                    tier=0,
                    category="management",
                    status="running",
                    image="ghcr.io/moghtech/komodo-core:latest",
                    image_tag="latest",
                    container_name="komodo",
                    host_port=9120,
                    last_healthy_at=int(_t_komodo.time()),
                )
        except Exception:  # noqa: S110  # best-effort DB update; provider result returned regardless
            pass

        return ProviderResult.success("No external hostnames.", data={})


# ---------------------------------------------------------------------------
# Portainer Business Edition
# ---------------------------------------------------------------------------


@register
class PortainerBEProvider(InfraProvider):
    slot = "management"
    key = "portainer_be"
    display_name = "Portainer Business Edition"

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
            "image": "portainer/portainer-ee:latest",  # EE = Business Edition
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
        return ProviderResult.success("Portainer Business Edition removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container("portainer")
        if not c or c.status != "running":
            return ProviderResult.failure("Portainer BE is not running.")
        return ProviderResult.success("Portainer Business Edition is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname registration needed.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Management provider — no hostname management.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("No external hostnames.", data={})
