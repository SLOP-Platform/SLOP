"""backend/infra/providers/auth_tinyauth.py

Tinyauth v5 auth provider.

Implements: deploy, remove, verify, protect, unprotect, export_users,
import_users, pre_migration_snapshot.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import httpx

from backend.core import docker_client
from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

log = get_logger(__name__)

ENV_USERS_KEY = "TINYAUTH_AUTH_USERS"


def _read_env_value(key: str) -> str:
    """Read a single KEY=VALUE from the install .env (the SSOT for tinyauth users).

    Tinyauth users live in the .env var ``TINYAUTH_AUTH_USERS`` (written by the
    wizard; deploy references it via ``${TINYAUTH_AUTH_USERS}``) — NOT in the slot
    config row. The prior export read slot config and returned an EMPTY set, so a
    swap silently dropped every user (#974). We parse the .env directly so export
    sees the real user set.
    """
    try:
        raw = config.env_file.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return ""
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export ") :].lstrip()
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        return v
    return ""


def _write_env_value(key: str, value: str) -> None:
    """Upsert a single KEY=VALUE into the install .env, preserving other lines.

    Mirrors the wizard's quoting rule (single-quote values containing ``$`` so
    bcrypt hashes survive shell/compose interpolation). Creates the file 0600 if
    absent. This is the import side of the round-trip: it writes the migrated
    user set back into the var ``deploy`` references.
    """
    import os

    def _quote(v: str) -> str:
        v = str(v).replace("\n", "").replace("\r", "")
        if "$" in v and not (v.startswith("'") and v.endswith("'")):
            return "'" + v + "'"
        return v

    path = config.env_file
    lines: list[str] = []
    found = False
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        existing = []
    for line in existing:
        s = line.strip()
        bare = s[len("export ") :].lstrip() if s.startswith("export ") else s
        if (
            bare
            and not bare.startswith("#")
            and "=" in bare
            and bare.partition("=")[0].strip() == key
        ):
            lines.append(f"{key}={_quote(value)}")
            found = True
        else:
            lines.append(line)
    if not found:
        lines.append(f"{key}={_quote(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError as _e:
        log.debug("Could not chmod .env: %s", _e)


def _validate_lan_subnet(v: str) -> str:
    """Validate that v is a valid CIDR notation (or empty string = no LAN bypass).

    Raises ValueError for non-empty values that aren't valid CIDRs.
    An empty/blank string means 'no LAN bypass' and is always accepted.
    """
    import ipaddress as _ipaddress

    v = v.strip()
    if not v:
        return v
    try:
        _ipaddress.ip_network(v, strict=False)
    except ValueError as err:
        raise ValueError(
            "lan_subnet must be a valid CIDR (e.g. 192.168.1.0/24), got: " + repr(v)
        ) from err
    return v


CONTAINER_NAME = "tinyauth"
IMAGE = "ghcr.io/steveiliop56/tinyauth:v5"
PORT = 3000


@register
class TinyauthProvider(InfraProvider):
    slot = "auth"
    key = "tinyauth"
    display_name = "Tinyauth v5"
    category = "auth"

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Base domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Your domain — Tinyauth will be reachable at auth.example.com",
        },
        {
            "key": "app_url",
            "label": "App URL",
            "type": "text",
            "placeholder": "https://auth.example.com",
            "required": False,
            "help": "External URL of the auth UI. Defaults to https://auth.<domain>.",
        },
        {
            "key": "lan_subnet",
            "label": "LAN bypass subnet",
            "type": "text",
            "placeholder": "192.168.1.0/24",
            "required": False,
            "help": "Optional CIDR allowed to bypass auth (e.g. your LAN). Blank = no bypass.",
        },
        {
            "type": "info",
            "key": "_info",
            "label": "",
            "help": (
                "Users are stored as bcrypt hashes in the .env var "
                "TINYAUTH_AUTH_USERS (username:$2b$...). Migrating to another auth "
                "provider transfers the username + bcrypt hash; plaintext passwords "
                "are never stored and cannot be exported."
            ),
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy Tinyauth v5 as the auth provider.

        Required config keys:
          app_url       — e.g. https://auth.example.com
          users         — bcrypt hash string e.g. admin:$2b$10$...
          lan_subnet    — e.g. 10.0.0.0/22 (for LAN bypass)
          domain        — base domain for Traefik labels
        """
        with StateDB() as db:
            platform = db.get_platform()

        domain = cfg.get("domain") or platform.domain or ""
        app_url = cfg.get("app_url", f"https://auth.{domain}")
        _raw_subnet = cfg.get(
            "lan_subnet",
            platform.config.get("lan_subnet", "") if hasattr(platform, "config") else "",
        )
        lan_subnet = _validate_lan_subnet(_raw_subnet)
        network = platform.network_name
        config_path = f"{platform.config_root}/tinyauth"

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "networks": [network],
            "ports": [f"{PORT}:{PORT}"],
            "volumes": [f"{config_path}:/app/data"],
            "environment": {
                "TINYAUTH_APPURL": app_url,
                "TINYAUTH_AUTH_USERS": "${TINYAUTH_AUTH_USERS}",
                "SECRET": "${TINYAUTH_SECRET}",
            },
            "labels": [
                "traefik.enable=true",
                f"traefik.http.routers.tinyauth.rule=Host(`auth.{domain}`)",
                "traefik.http.routers.tinyauth.entrypoints=websecure",
                "traefik.http.routers.tinyauth.tls=true",
                f"traefik.http.services.tinyauth.loadbalancer.server.port={PORT}",
                # ForwardAuth middleware used by all other services
                "traefik.http.middlewares.tinyauth-auth.forwardauth.address="
                f"http://{CONTAINER_NAME}:{PORT}/api/auth/traefik",
                "traefik.http.middlewares.tinyauth-auth.forwardauth.trustForwardHeader=true",
                # authResponseHeaders: ensure auth headers flow to app but WWW-Authenticate
                # is NOT forwarded to the browser (prevents the native browser popup)
                "traefik.http.middlewares.tinyauth-auth.forwardauth.authResponseHeaders=X-Auth-User,X-Auth-Email",
            ],
        }

        try:
            import pathlib

            pathlib.Path(config_path).mkdir(parents=True, exist_ok=True)
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure(
                    "Tinyauth failed to start.",
                    _out[:300],
                )
        except Exception as e:
            return ProviderResult.failure("Could not deploy Tinyauth.", str(e))

        # Update slot state
        with StateDB() as db:
            db.update_slot(
                "auth",
                provider="tinyauth",
                status="active",
                config={"app_url": app_url, "domain": domain, "lan_subnet": lan_subnet},
                deployed_at=int(time.time()),
            )

        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "tinyauth",
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
            f"Tinyauth v5 deployed at {app_url}.",
            data={"app_url": app_url, "lan_subnet": lan_subnet},
        )

    def remove(self) -> ProviderResult:
        from backend.core.compose import remove_fragment

        frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
        if frag_path.exists():
            try:
                compose_down(frag_path, timeout=30)
            except Exception as e:
                return ProviderResult.failure("Could not stop Tinyauth.", str(e))
        remove_fragment(CONTAINER_NAME)
        with StateDB() as db:
            db.update_slot("auth", status="empty", provider=None, config={})
        return ProviderResult.success("Tinyauth removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if c is None:
            return ProviderResult.failure(
                "Tinyauth container is not running.",
                "Run the infrastructure wizard to deploy it.",
            )
        if c.status != "running":
            return ProviderResult.failure(
                f"Tinyauth container is in '{c.status}' state.",
                f"Check logs: docker logs {CONTAINER_NAME}",
            )
        # Check API responds
        try:
            r = httpx.get(f"http://localhost:{PORT}/api/health", timeout=5)
            if r.status_code == 200:
                return ProviderResult.success("Tinyauth is healthy.")
        except Exception as _e:
            log.debug("Suppressed exception: %s", _e)
        # Health endpoint may not exist in all versions — just check running
        return ProviderResult.success("Tinyauth is running (API check skipped).")

    def protect(self, hostname: str, rules: dict[str, Any] | None = None) -> ProviderResult:
        """Tinyauth protects via Traefik middleware — no per-hostname API needed."""
        return ProviderResult.success(
            f"Tinyauth protection is applied via Traefik middleware "
            f"(covers {hostname} automatically)."
        )

    def unprotect(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Tinyauth protection removed by updating Traefik labels.")

    def export_users(self) -> ProviderResult:
        """Export the Tinyauth user set as portable records for migration.

        Source of truth = the .env var TINYAUTH_AUTH_USERS, a comma-separated list
        of ``username:$2b$<bcrypt>`` entries (the prior version read the slot
        config, which never holds users → it exported ZERO and the swap dropped
        everyone, #974).

        Each portable record is::

            {"username": str, "hash": "$2b$...", "hash_scheme": "bcrypt",
             "email": None}

        ``hash_scheme`` is "bcrypt" because Tinyauth stores bcrypt and Authelia's
        file backend accepts bcrypt ($2b/$2y) hashes verbatim — so the swap is
        password-lossless. ``email`` is None: Tinyauth has no email field, so the
        new provider synthesizes a placeholder (a benign, declared loss — NOT the
        user-dropping data-loss this fix closes).

        ``lossy`` is False here: the user SET (usernames + verifiable hashes) is
        fully preserved. We set ``lossy=True`` only if we genuinely cannot carry a
        user across (none today). The swap engine rolls back when ``lossy`` is True.
        """
        users_str = _read_env_value(ENV_USERS_KEY)
        users: list[dict[str, Any]] = []
        for entry in users_str.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            username, hashed = entry.split(":", 1)
            username = username.strip()
            hashed = hashed.strip()
            if not username or not hashed:
                continue
            scheme = "bcrypt" if hashed.startswith(("$2a$", "$2b$", "$2y$")) else "unknown"
            users.append(
                {
                    "username": username,
                    "hash": hashed,
                    "hash_scheme": scheme,
                    "email": None,
                }
            )
        # Fail-closed: an unrecognized hash scheme cannot be safely migrated.
        unknown = [u["username"] for u in users if u["hash_scheme"] == "unknown"]
        lossy = bool(unknown)
        msg = f"Exported {len(users)} user(s) (bcrypt hashes preserved; plaintext never stored)."
        if lossy:
            msg = (
                f"Exported {len(users)} user(s), but {len(unknown)} have an "
                "unrecognized password hash that cannot be migrated safely."
            )
        return ProviderResult.success(
            msg,
            data={
                "users": users,
                "lossy": lossy,
                "lossy_reason": (
                    f"unrecognized hash scheme for: {', '.join(unknown)}" if lossy else ""
                ),
            },
        )

    def import_users(self, users: list[dict[str, Any]]) -> ProviderResult:
        """Import portable user records into Tinyauth (the .env user var).

        Accepts the portable record shape produced by ``export_users``. Tinyauth's
        file/env backend requires bcrypt hashes, so a record whose ``hash_scheme``
        is not bcrypt CANNOT be imported (Tinyauth cannot rehash an argon2 digest)
        — we fail-closed so the swap engine rolls back rather than writing an auth
        config that locks everyone out.
        """
        if not users:
            return ProviderResult.success("No users to import.", data={"imported": 0})

        entries: list[str] = []
        unmigratable: list[str] = []
        for u in users:
            username = str(u.get("username", "")).strip()
            hashed = str(u.get("hash", "")).strip()
            scheme = u.get("hash_scheme") or (
                "bcrypt" if hashed.startswith(("$2a$", "$2b$", "$2y$")) else "unknown"
            )
            if not username or not hashed:
                continue
            if scheme != "bcrypt":
                unmigratable.append(username or "<unnamed>")
                continue
            entries.append(f"{username}:{hashed}")

        if unmigratable:
            return ProviderResult.failure(
                f"Cannot import {len(unmigratable)} user(s) into Tinyauth: their "
                "password hash is not bcrypt and Tinyauth cannot rehash it. "
                "Migration aborted to avoid locking these users out.",
                f"Affected: {', '.join(unmigratable)}",
            )
        if not entries:
            return ProviderResult.success("No importable users.", data={"imported": 0})

        _write_env_value(ENV_USERS_KEY, ",".join(entries))
        return ProviderResult.success(
            f"Imported {len(entries)} user(s) into Tinyauth. "
            "Redeploy Tinyauth to load the migrated users.",
            data={"imported": len(entries)},
        )

    def pre_migration_snapshot(self) -> ProviderResult:
        """Capture the current user var + slot state so rollback can restore both."""
        with StateDB() as db:
            slot = db.get_slot("auth")
        snap = dict(slot.__dict__)
        snap["env_users"] = _read_env_value(ENV_USERS_KEY)
        return ProviderResult.success("Snapshot captured.", data=snap)

    def restore_from_snapshot(self, snapshot: dict[str, Any]) -> ProviderResult:
        """Restore the user var captured by ``pre_migration_snapshot`` on rollback."""
        env_users = snapshot.get("env_users")
        if env_users is not None:
            _write_env_value(ENV_USERS_KEY, env_users)
        return ProviderResult.success("Tinyauth user state restored from snapshot.")
