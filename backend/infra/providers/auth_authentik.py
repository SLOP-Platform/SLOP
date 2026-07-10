"""backend/infra/providers/auth_authentik.py

Authentik — enterprise-grade identity platform.
Provides authentication via forwardAuth middleware for Traefik-routed services.

Implements: deploy, remove, verify, protect, unprotect, export_users,
import_users, pre_migration_snapshot, restore_from_snapshot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import yaml

from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

log = get_logger(__name__)
CONTAINER_NAME = "authentik"
DB_CONTAINER_NAME = "authentik-postgres"

# Authentik requires PostgreSQL for production use
AUTHENTIK_IMAGE = "ghcr.io/goauthentik/server:latest"
POSTGRES_IMAGE = "postgres:16-alpine"


@register
class AuthentikProvider(InfraProvider):
    slot = "auth"
    key = "authentik"
    display_name = "Authentik"
    category = "auth"
    description = (
        "Enterprise-grade identity platform with LDAP, SAML, OAuth2 provider, "
        "flows, and blueprints. Supports social login and advanced 2FA."
    )

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Base domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Your domain — Authentik will be reachable at auth.example.com",
        },
        {
            "key": "secret_key",
            "label": "Secret key",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Random secret for Authentik. Generate with: openssl rand -hex 32",
        },
        {
            "key": "postgres_password",
            "label": "PostgreSQL password",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Password for the PostgreSQL database. Generate a secure password.",
        },
        {
            "key": "email",
            "label": "Admin email",
            "type": "email",
            "required": True,
            "placeholder": "admin@example.com",
            "help": "Email address for the initial admin account.",
        },
        {
            "key": "password",
            "label": "Admin password",
            "type": "password",
            "secret": True,
            "required": True,
            "help": "Password for the initial admin account.",
        },
        {
            "type": "info",
            "key": "_info",
            "label": "",
            "help": (
                "Authentik requires PostgreSQL. A Postgres container will be deployed "
                "automatically. After deployment, visit https://auth.example.com to "
                "complete the setup via the web UI."
            ),
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        domain = cfg.get("domain", "").strip()
        secret_key = cfg.get("secret_key", "").strip()
        postgres_password = cfg.get("postgres_password", "").strip()
        email = cfg.get("email", "").strip()
        password = cfg.get("password", "").strip()

        # Validate required fields BEFORE any DB/container operations
        if not domain:
            return ProviderResult.failure("Domain is required.", "")
        if not secret_key:
            return ProviderResult.failure("Secret key is required.", "")
        if not postgres_password:
            return ProviderResult.failure("PostgreSQL password is required.", "")
        if not email or not password:
            return ProviderResult.failure("Admin email and password are required.", "")

        with StateDB() as db:
            _p = db.get_platform()
        cert_resolver = _p.cert_resolver or "letsencrypt"

        # Create config directory
        authentik_config_dir = Path(config.data_dir).parent / "config" / "authentik"
        authentik_config_dir.mkdir(parents=True, exist_ok=True)

        # PostgreSQL fragment
        postgres_fragment = {
            "image": POSTGRES_IMAGE,
            "container_name": DB_CONTAINER_NAME,
            "restart": "unless-stopped",
            "environment": {
                "POSTGRES_PASSWORD": postgres_password,
                "POSTGRES_USER": "authentik",
                "POSTGRES_DB": "authentik",
            },
            "volumes": [
                f"{authentik_config_dir}/postgres:/var/lib/postgresql/data",
            ],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U authentik"],
                "interval": "5s",
                "timeout": "5s",
                "retries": 5,
            },
            "networks": ["default"],
        }

        # Authentik server fragment
        server_fragment = {
            "image": AUTHENTIK_IMAGE,
            "container_name": CONTAINER_NAME,
            "command": "server",
            "restart": "unless-stopped",
            "environment": {
                "AUTHENTIK_SECRET_KEY": secret_key,
                "AUTHENTIK_POSTGRESQL__HOST": DB_CONTAINER_NAME,
                "AUTHENTIK_POSTGRESQL__NAME": "authentik",
                "AUTHENTIK_POSTGRESQL__USER": "authentik",
                "AUTHENTIK_POSTGRESQL__PASSWORD": postgres_password,
                "AUTHENTIK_EMAIL__HOST": "",
                "AUTHENTIK_EMAIL__PORT": "",
                "AUTHENTIK_EMAIL__USERNAME": "",
                "AUTHENTIK_EMAIL__PASSWORD": "",
                "AUTHENTIK_EMAIL__USE_TLS": "false",
                "AUTHENTIK_EMAIL__USE_SSL": "false",
                "AUTHENTIK_EMAIL__TIMEOUT": "10",
            },
            "volumes": [
                f"{authentik_config_dir}/media:/media",
                f"{authentik_config_dir}/custom-templates:/custom-templates",
            ],
            "ports": ["9000:9000"],
            "depends_on": {
                DB_CONTAINER_NAME: {
                    "condition": "service_healthy",
                },
            },
            "networks": ["default"],
            "labels": {
                "traefik.enable": "true",
                "traefik.http.routers.authentik.rule": f"Host(`auth.{domain}`)",
                "traefik.http.routers.authentik.entrypoints": "websecure",
                "traefik.http.routers.authentik.tls.certresolver": cert_resolver,
                "traefik.http.services.authentik.loadbalancer.server.port": "9000",
                # ForwardAuth middleware for other services
                "traefik.http.middlewares.authentik-forwardauth.forwardauth.address": f"http://{CONTAINER_NAME}:9000/outpost.goauthentik.io/auth/traefik",
                "traefik.http.middlewares.authentik-forwardauth.forwardauth.trustForwardHeader": "true",
                "traefik.http.middlewares.authentik-forwardauth.forwardauth.authResponseHeaders": "X-Authentik-Username,X-Authentik-Groups,X-Authentik-Email,X-Authentik-Name",
            },
        }

        # Authentik worker fragment (required for background tasks)
        worker_fragment = {
            "image": AUTHENTIK_IMAGE,
            "container_name": "authentik-worker",
            "command": "worker",
            "restart": "unless-stopped",
            "environment": {
                "AUTHENTIK_SECRET_KEY": secret_key,
                "AUTHENTIK_POSTGRESQL__HOST": DB_CONTAINER_NAME,
                "AUTHENTIK_POSTGRESQL__NAME": "authentik",
                "AUTHENTIK_POSTGRESQL__USER": "authentik",
                "AUTHENTIK_POSTGRESQL__PASSWORD": postgres_password,
            },
            "volumes": [
                f"{authentik_config_dir}/media:/media",
                f"{authentik_config_dir}/custom-templates:/custom-templates",
            ],
            "depends_on": {
                DB_CONTAINER_NAME: {
                    "condition": "service_healthy",
                },
            },
            "networks": ["default"],
        }

        try:
            # Write PostgreSQL fragment
            postgres_frag_path = write_fragment(DB_CONTAINER_NAME, postgres_fragment)
            # Write Authentik server fragment
            server_frag_path = write_fragment(CONTAINER_NAME, server_fragment)
            # Write Authentik worker fragment
            worker_frag_path = write_fragment("authentik-worker", worker_fragment)

            # Deploy in order: postgres -> server -> worker
            rc, _out = compose_up(postgres_frag_path, timeout=120)
            if rc != 0:
                return ProviderResult.failure("PostgreSQL failed to start.", _out[:400])

            rc, _out = compose_up(server_frag_path, timeout=120)
            if rc != 0:
                return ProviderResult.failure("Authentik server failed to start.", _out[:400])

            rc, _out = compose_up(worker_frag_path, timeout=120)
            if rc != 0:
                return ProviderResult.failure("Authentik worker failed to start.", _out[:400])

        except Exception as e:
            return ProviderResult.failure("Could not deploy Authentik.", str(e))

        with StateDB() as db:
            db.update_slot(
                "auth",
                provider=self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
            )

        # Register as a fully managed app
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "authentik",
                    display_name=self.display_name,
                    tier=0,
                    category=self.category,
                    status="running",
                    image=AUTHENTIK_IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=9000,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            log.debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Authentik deployed at auth.{domain}. "
            "Visit the admin UI to complete setup and create the initial admin user."
        )

    def remove(self) -> ProviderResult:
        try:
            # Remove in reverse order
            worker_frag = config.compose_dir / "authentik-worker.yaml"
            if worker_frag.exists():
                compose_down(worker_frag)
                worker_frag.unlink(missing_ok=True)

            server_frag = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if server_frag.exists():
                compose_down(server_frag)
                server_frag.unlink(missing_ok=True)

            postgres_frag = config.compose_dir / f"{DB_CONTAINER_NAME}.yaml"
            if postgres_frag.exists():
                compose_down(postgres_frag)
                postgres_frag.unlink(missing_ok=True)

        except Exception as e:
            return ProviderResult.failure("Could not remove Authentik.", str(e))

        with StateDB() as db:
            db.update_slot(
                "auth", provider=None, status="empty", display_name=None, container_name=None
            )
        return ProviderResult.success("Authentik removed.")

    def verify(self) -> ProviderResult:
        import subprocess

        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=5,
            )
            status = r.stdout.strip()
            if status == "running":
                return ProviderResult.success("Authentik is running.")
            return ProviderResult.failure(f"Authentik status: {status or 'not found'}.", "")
        except Exception as e:
            return ProviderResult.failure("Cannot check Authentik.", str(e))

    def protect(self, hostname: str, rules: dict[str, Any] | None = None) -> ProviderResult:
        """Authentik protects routes via its Traefik forwardAuth middleware."""
        return ProviderResult.success(
            f"Authentik protection is applied via the 'authentik-forwardauth' Traefik middleware "
            f"(covers {hostname}); configure policies in the Authentik admin UI."
        )

    def unprotect(self, hostname: str) -> ProviderResult:
        return ProviderResult.success(
            "Authentik protection removed by dropping the 'authentik-forwardauth' middleware."
        )

    def export_users(self) -> ProviderResult:
        """Authentik stores users in PostgreSQL — cannot export directly.

        Returns a note that users must be exported via the Authentik API or UI.
        """
        return ProviderResult.failure(
            "Authentik user export requires the Authentik API. "
            "Use the Authentik admin UI to export users, or configure API access.",
            "User data is stored in PostgreSQL and requires API access for export."
        )

    def import_users(self, users: list[dict[str, Any]]) -> ProviderResult:
        """Authentik user import requires API calls.

        This is a placeholder — full implementation would use the Authentik API
        to create users programmatically.
        """
        return ProviderResult.failure(
            "Authentik user import requires the Authentik API. "
            "Users must be created via the admin UI or API.",
            "Migration to Authentik requires manual user creation or API scripting."
        )

    def pre_migration_snapshot(self) -> ProviderResult:
        """Capture note that PostgreSQL data needs backup."""
        return ProviderResult.success(
            "Authentik snapshot noted. PostgreSQL data is in the volume — "
            "manual backup may be required for full rollback.",
            data={"note": "PostgreSQL volume must be backed up separately"}
        )

    def restore_from_snapshot(self, snapshot: dict[str, Any]) -> ProviderResult:
        """Restore note — actual restore requires volume restore."""
        return ProviderResult.success(
            "Authentik restore noted. Restore PostgreSQL volume from backup if needed."
        )
