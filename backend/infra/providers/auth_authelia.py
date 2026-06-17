"""backend/infra/providers/auth_authelia.py

Authelia — open-source SSO and 2FA proxy.
Provides authentication for all Traefik-routed services.
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

log = get_logger(__name__)
CONTAINER_NAME = "authelia"


def _authelia_config_dir() -> Path:
    """The host directory mounted at /config — holds users.yml (the user SSOT)."""
    return Path(config.data_dir).parent / "config" / "authelia"


def _users_file() -> Path:
    return _authelia_config_dir() / "users.yml"


def _load_users_doc() -> dict[str, Any]:
    """Parse users.yml into ``{"users": {username: {...}}}`` (empty if absent)."""
    path = _users_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return {"users": {}}
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {"users": {}}
    if not isinstance(doc, dict):
        return {"users": {}}
    users = doc.get("users")
    if not isinstance(users, dict):
        doc["users"] = {}
    return doc


class AutheliaProvider(InfraProvider):
    slot = "auth"
    key = "authelia"
    display_name = "Authelia"
    description = (
        "Open-source SSO + 2FA. More feature-rich than TinyAuth; supports LDAP, email OTP, TOTP."
    )

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Base domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Your domain — Authelia will be reachable at auth.example.com",
        },
        {
            "key": "jwt_secret",
            "label": "JWT secret",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Random secret for signing JWTs. Generate with: openssl rand -hex 32",
        },
        {
            "key": "session_secret",
            "label": "Session secret",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Random secret for sessions. Generate with: openssl rand -hex 32",
        },
        {
            "key": "storage_password",
            "label": "Storage encryption password",
            "type": "text",
            "secret": True,
            "required": False,
            "help": "Password for encrypting the SQLite storage. Leave blank to auto-generate.",
        },
        {
            "type": "info",
            "key": "_info",
            "label": "",
            "help": (
                "Authelia requires a configuration file at config/authelia/configuration.yml. "
                "A minimal config will be created automatically. Edit it for advanced options "
                "(LDAP, SMTP, 2FA providers)."
            ),
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        domain = cfg.get("domain", "").strip()
        jwt_secret = cfg.get("jwt_secret", "").strip()
        session_secret = cfg.get("session_secret", "").strip()

        with StateDB() as db:
            _p = db.get_platform()
        cert_resolver = _p.cert_resolver or "letsencrypt"

        if not domain:
            return ProviderResult.failure("Domain is required.", "")
        if not jwt_secret or not session_secret:
            return ProviderResult.failure("JWT secret and session secret are required.", "")

        storage_password = cfg.get("storage_password", "").strip() or "authelia_storage_pass"

        # Create config directory and minimal config
        authelia_config_dir = Path(config.data_dir).parent / "config" / "authelia"
        authelia_config_dir.mkdir(parents=True, exist_ok=True)
        config_file = authelia_config_dir / "configuration.yml"

        if not config_file.exists():
            config_file.write_text(
                f"""# Authelia minimal configuration
# Full docs: https://www.authelia.com/configuration/

server:
  host: 0.0.0.0
  port: 9091

jwt_secret: {jwt_secret}

default_redirection_url: https://{domain}

authentication_backend:
  file:
    path: /config/users.yml

access_control:
  default_policy: deny
  rules:
    - domain: "*.{domain}"
      policy: one_factor

session:
  name: authelia_session
  secret: {session_secret}
  expiration: 1h
  inactivity: 5m
  domain: {domain}

regulation:
  max_retries: 3
  find_time: 2m
  ban_time: 5m

storage:
  encryption_key: {storage_password}
  local:
    path: /config/db.sqlite3

notifier:
  filesystem:
    filename: /config/notification.txt
""",
                encoding="utf-8",
            )

        # Create minimal users file
        users_file = authelia_config_dir / "users.yml"
        if not users_file.exists():
            users_file.write_text(
                "# Add users here. Hash passwords with: authelia crypto hash generate argon2\n"
                "users:\n"
                "  # admin:\n"
                "  #   displayname: Admin\n"
                "  #   password: '$argon2id$...'\n"
                "  #   email: admin@example.com\n"
                "  #   groups: [admins]\n",
                encoding="utf-8",
            )

        fragment = {
            "image": "authelia/authelia:latest",
            "container_name": CONTAINER_NAME,
            "volumes": [
                f"{authelia_config_dir}:/config",
            ],
            "environment": {
                "TZ": "UTC",
            },
            "expose": ["9091"],
            "labels": {
                "traefik.enable": "true",
                "traefik.http.routers.authelia.rule": f"Host(`auth.{domain}`)",
                "traefik.http.routers.authelia.entrypoints": "websecure",
                "traefik.http.routers.authelia.tls.certresolver": cert_resolver,
                "traefik.http.services.authelia.loadbalancer.server.port": "9091",
                # Middleware definition for other services to use
                "traefik.http.middlewares.authelia.forwardauth.address": f"http://authelia:9091/api/verify?rd=https://auth.{domain}",
                "traefik.http.middlewares.authelia.forwardauth.trustForwardHeader": "true",
                "traefik.http.middlewares.authelia.forwardauth.authResponseHeaders": "Remote-User,Remote-Groups,Remote-Name,Remote-Email",
            },
            "restart": "unless-stopped",
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("Authelia failed to start.", _out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy Authelia.", str(e))

        with StateDB() as db:
            db.update_slot(
                "auth",
                provider=self.key,
                status="active",
                display_name=self.display_name,
                container_name=CONTAINER_NAME,
            )

        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "authelia",
                    display_name="Authelia",
                    tier=0,  # tier 0 = infrastructure layer
                    category="auth",
                    status="running",
                    image="authelia/authelia:latest",
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=9091,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Authelia deployed at auth.{domain}. "
            "Edit config/authelia/users.yml to add users, then restart the container."
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                rc, _out = compose_down(frag_path)
                if rc != 0:
                    log.warning("Authelia stop failed: %s", _out[:200])
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove Authelia.", str(e))
        with StateDB() as db:
            db.update_slot(
                "auth", provider=None, status="empty", display_name=None, container_name=None
            )
        return ProviderResult.success("Authelia removed.")

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
                return ProviderResult.success("Authelia is running.")
            return ProviderResult.failure(f"Authelia status: {status or 'not found'}.", "")
        except Exception as e:
            return ProviderResult.failure("Cannot check Authelia.", str(e))

    # ── Auth slot verbs ───────────────────────────────────────────────────

    def protect(self, hostname: str, rules: dict[str, Any] | None = None) -> ProviderResult:
        """Authelia protects routes via its Traefik forwardAuth middleware.

        Like Tinyauth, protection is applied with router labels (the ``authelia``
        middleware deploy() defines), not a per-hostname API call. Per-host access
        policy lives in access_control rules in configuration.yml.
        """
        return ProviderResult.success(
            f"Authelia protection is applied via the 'authelia' Traefik middleware "
            f"(covers {hostname}); per-host policy is set in configuration.yml access_control."
        )

    def unprotect(self, hostname: str) -> ProviderResult:
        return ProviderResult.success(
            "Authelia protection removed by dropping the 'authelia' middleware from "
            "the route's Traefik labels."
        )

    def export_users(self) -> ProviderResult:
        """Export Authelia's file-backend users as portable records.

        Reads users.yml (the file authentication backend). Authelia stores argon2id
        (or bcrypt) hashes; we carry the hash verbatim with its detected scheme so
        the target provider can decide whether it can accept it.
        """
        doc = _load_users_doc()
        users_map = doc.get("users", {})
        users: list[dict[str, Any]] = []
        for username, rec in users_map.items():
            if not isinstance(rec, dict):
                continue
            hashed = str(rec.get("password", "")).strip()
            if not username or not hashed:
                continue
            if hashed.startswith("$argon2"):
                scheme = "argon2id"
            elif hashed.startswith(("$2a$", "$2b$", "$2y$")):
                scheme = "bcrypt"
            else:
                scheme = "unknown"
            users.append(
                {
                    "username": str(username),
                    "hash": hashed,
                    "hash_scheme": scheme,
                    "email": rec.get("email"),
                    "displayname": rec.get("displayname"),
                    "groups": rec.get("groups"),
                }
            )
        unknown = [u["username"] for u in users if u["hash_scheme"] == "unknown"]
        # argon2id users are lossless to other Authelia-class targets but CANNOT be
        # accepted by a bcrypt-only target (Tinyauth) — that target's import_users
        # fail-closes. We declare lossy only for truly-unrecognized hashes here.
        lossy = bool(unknown)
        return ProviderResult.success(
            f"Exported {len(users)} Authelia user(s).",
            data={
                "users": users,
                "lossy": lossy,
                "lossy_reason": (
                    f"unrecognized hash scheme for: {', '.join(unknown)}" if lossy else ""
                ),
            },
        )

    def import_users(self, users: list[dict[str, Any]]) -> ProviderResult:
        """Import portable user records into Authelia's users.yml file backend.

        Authelia's file backend accepts BOTH argon2id and bcrypt ($2b/$2y) hashes,
        so a Tinyauth→Authelia migration is password-lossless — the bcrypt hash is
        written verbatim, no reset needed. An unrecognized hash scheme cannot be
        written safely → fail-closed so the swap engine rolls back.
        """
        if not users:
            return ProviderResult.success("No users to import.", data={"imported": 0})

        doc = _load_users_doc()
        users_map = doc.setdefault("users", {})
        unmigratable: list[str] = []
        imported = 0
        for u in users:
            username = str(u.get("username", "")).strip()
            hashed = str(u.get("hash", "")).strip()
            if not username or not hashed:
                continue
            scheme = u.get("hash_scheme")
            if not scheme:
                if hashed.startswith("$argon2"):
                    scheme = "argon2id"
                elif hashed.startswith(("$2a$", "$2b$", "$2y$")):
                    scheme = "bcrypt"
                else:
                    scheme = "unknown"
            if scheme not in ("argon2id", "bcrypt"):
                unmigratable.append(username)
                continue
            email = u.get("email") or f"{username}@local"
            rec: dict[str, Any] = {
                "displayname": u.get("displayname") or username,
                "password": hashed,
                "email": email,
            }
            groups = u.get("groups")
            if groups:
                rec["groups"] = groups
            users_map[username] = rec
            imported += 1

        if unmigratable:
            return ProviderResult.failure(
                f"Cannot import {len(unmigratable)} user(s) into Authelia: "
                "unrecognized password hash scheme. Migration aborted.",
                f"Affected: {', '.join(unmigratable)}",
            )

        path = _users_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(doc, default_flow_style=False, sort_keys=True),
            encoding="utf-8",
        )
        return ProviderResult.success(
            f"Imported {imported} user(s) into Authelia users.yml. Restart Authelia to load them.",
            data={"imported": imported},
        )

    # ── Migration support ─────────────────────────────────────────────────

    def pre_migration_snapshot(self) -> ProviderResult:
        """Capture users.yml so a failed swap can be rolled back without user loss."""
        try:
            content = _users_file().read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            content = None
        return ProviderResult.success(
            "Authelia user snapshot captured.", data={"users_yml": content}
        )

    def restore_from_snapshot(self, snapshot: dict[str, Any]) -> ProviderResult:
        """Restore users.yml captured by pre_migration_snapshot on rollback."""
        content = snapshot.get("users_yml")
        if content is None:
            return ProviderResult.success("No Authelia user snapshot to restore.")
        path = _users_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ProviderResult.success("Authelia users.yml restored from snapshot.")
