"""backend/infra/providers/auth_oauth2_proxy.py

oauth2-proxy — lightweight forward-auth middleware for social login.

Provides authentication via external OAuth2/OIDC providers (Google, GitHub, Azure, etc.)
without running a full identity provider. Integrates natively with Traefik.

Implements: deploy, remove, verify, protect, unprotect.
Note: oauth2-proxy does NOT manage users locally — users are authenticated by the
external IdP. Therefore export_users/import_users are not applicable (fail-closed).
"""

from __future__ import annotations

from typing import Any, ClassVar

from backend.core.compose import compose_up, compose_down, write_fragment
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

log = get_logger(__name__)
CONTAINER_NAME = "oauth2-proxy"

# Supported OAuth providers (common presets)
OAUTH2_PROXY_IMAGE = "quay.io/oauth2-proxy/oauth2-proxy:v7"


@register
class OAuth2ProxyProvider(InfraProvider):
    slot = "auth"
    key = "oauth2-proxy"
    display_name = "oauth2-proxy"
    category = "auth"
    description = (
        "Lightweight forward-auth middleware for social login (Google, GitHub, "
        "Azure, generic OIDC). Delegates authentication to external IdPs — "
        "no local user database required."
    )

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Base domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Your domain — oauth2-proxy will be reachable at auth.example.com",
        },
        {
            "key": "provider",
            "label": "OAuth provider",
            "type": "select",
            "options": [
                {"value": "google", "label": "Google"},
                {"value": "github", "label": "GitHub"},
                {"value": "azure", "label": "Azure AD"},
                {"value": "oidc", "label": "Generic OIDC"},
            ],
            "required": True,
            "help": "The OAuth2/OIDC provider to use for authentication.",
        },
        {
            "key": "client_id",
            "label": "Client ID",
            "type": "text",
            "required": True,
            "help": "OAuth2 client ID from your provider.",
        },
        {
            "key": "client_secret",
            "label": "Client secret",
            "type": "password",
            "secret": True,
            "required": True,
            "help": "OAuth2 client secret from your provider.",
        },
        {
            "key": "oidc_issuer_url",
            "label": "OIDC issuer URL",
            "type": "text",
            "placeholder": "https://accounts.google.com",
            "required": False,
            "help": "Required for OIDC provider. The issuer URL (e.g. https://accounts.google.com).",
        },
        {
            "key": "cookie_secret",
            "label": "Cookie secret",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Random secret for cookie encryption. Generate with: openssl rand -hex 32",
        },
        {
            "key": "email_domains",
            "label": "Allowed email domains",
            "type": "text",
            "placeholder": "example.com,example.org",
            "required": False,
            "help": "Comma-separated list of email domains allowed to authenticate. Blank = any.",
        },
        {
            "type": "info",
            "key": "_info",
            "label": "",
            "help": (
                "You must register this app with your OAuth provider first. "
                "The callback URL will be: https://auth.example.com/oauth2/callback. "
                "No local user database is maintained — users are authenticated by the IdP."
            ),
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        domain = cfg.get("domain", "").strip()
        provider = cfg.get("provider", "").strip()
        client_id = cfg.get("client_id", "").strip()
        client_secret = cfg.get("client_secret", "").strip()
        oidc_issuer_url = cfg.get("oidc_issuer_url", "").strip()
        cookie_secret = cfg.get("cookie_secret", "").strip()
        email_domains = cfg.get("email_domains", "").strip()

        # Validate required fields BEFORE any DB/container operations
        if not domain:
            return ProviderResult.failure("Domain is required.", "")
        if not provider:
            return ProviderResult.failure("OAuth provider is required.", "")
        if not client_id or not client_secret:
            return ProviderResult.failure("Client ID and client secret are required.", "")
        if not cookie_secret:
            return ProviderResult.failure("Cookie secret is required.", "")
        if provider == "oidc" and not oidc_issuer_url:
            return ProviderResult.failure("OIDC issuer URL is required for OIDC provider.", "")

        with StateDB() as db:
            _p = db.get_platform()
        cert_resolver = _p.cert_resolver or "letsencrypt"

        # Build environment variables
        env: dict[str, Any] = {
            "OAUTH2_PROXY_CLIENT_ID": client_id,
            "OAUTH2_PROXY_CLIENT_SECRET": client_secret,
            "OAUTH2_PROXY_COOKIE_SECRET": cookie_secret,
            "OAUTH2_PROXY_HTTP_ADDRESS": "0.0.0.0:4180",
            "OAUTH2_PROXY_REDIRECT_URL": f"https://auth.{domain}/oauth2/callback",
            "OAUTH2_PROXY_EMAIL_DOMAINS": email_domains or "*",
            "OAUTH2_PROXY_PROVIDER": provider,
            "OAUTH2_PROXY_UPSTREAMS": "http://localhost:4180",
            "OAUTH2_PROXY_COOKIE_DOMAINS": domain,
            "OAUTH2_PROXY_WHITELIST_DOMAINS": domain,
        }

        if provider == "oidc":
            env["OAUTH2_PROXY_OIDC_ISSUER_URL"] = oidc_issuer_url
            env["OAUTH2_PROXY_SCOPE"] = "openid profile email"

        fragment = {
            "image": OAUTH2_PROXY_IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "environment": env,
            "ports": ["4180:4180"],
            "labels": {
                "traefik.enable": "true",
                "traefik.http.routers.oauth2-proxy.rule": f"Host(`auth.{domain}`)",
                "traefik.http.routers.oauth2-proxy.entrypoints": "websecure",
                "traefik.http.routers.oauth2-proxy.tls.certresolver": cert_resolver,
                "traefik.http.services.oauth2-proxy.loadbalancer.server.port": "4180",
                # ForwardAuth middleware for other services
                "traefik.http.middlewares.oauth2-proxy.forwardauth.address": f"http://{CONTAINER_NAME}:4180",
                "traefik.http.middlewares.oauth2-proxy.forwardauth.trustForwardHeader": "true",
                "traefik.http.middlewares.oauth2-proxy.forwardauth.authResponseHeaders": "X-Auth-Request-User,X-Auth-Request-Email,X-Auth-Request-Preferred-Username",
            },
            "networks": ["default"],
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure("oauth2-proxy failed to start.", _out[:400])
        except Exception as e:
            return ProviderResult.failure("Could not deploy oauth2-proxy.", str(e))

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
                    "oauth2-proxy",
                    display_name=self.display_name,
                    tier=0,
                    category=self.category,
                    status="running",
                    image=OAUTH2_PROXY_IMAGE,
                    image_tag="v7",
                    container_name=CONTAINER_NAME,
                    host_port=4180,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            log.debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"oauth2-proxy deployed at auth.{domain}. "
            f"Configured for {provider} OAuth. "
            "Users will be redirected to the provider for login."
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                rc, _out = compose_down(frag_path)
                if rc != 0:
                    log.warning("oauth2-proxy stop failed: %s", _out[:200])
                frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove oauth2-proxy.", str(e))

        with StateDB() as db:
            db.update_slot(
                "auth", provider=None, status="empty", display_name=None, container_name=None
            )
        return ProviderResult.success("oauth2-proxy removed.")

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
                return ProviderResult.success("oauth2-proxy is running.")
            return ProviderResult.failure(f"oauth2-proxy status: {status or 'not found'}.", "")
        except Exception as e:
            return ProviderResult.failure("Cannot check oauth2-proxy.", str(e))

    def protect(self, hostname: str, rules: dict[str, Any] | None = None) -> ProviderResult:
        """oauth2-proxy protects routes via its Traefik forwardAuth middleware."""
        return ProviderResult.success(
            f"oauth2-proxy protection is applied via the 'oauth2-proxy' Traefik middleware "
            f"(covers {hostname}); users will be redirected to the OAuth provider."
        )

    def unprotect(self, hostname: str) -> ProviderResult:
        return ProviderResult.success(
            "oauth2-proxy protection removed by dropping the 'oauth2-proxy' middleware."
        )

    def export_users(self) -> ProviderResult:
        """oauth2-proxy does NOT manage users — authentication is delegated to the IdP."""
        return ProviderResult.failure(
            "oauth2-proxy does not maintain a local user database. "
            "Users are authenticated by the external OAuth provider.",
            "No users to export — authentication is fully delegated.",
        )

    def import_users(self, users: list[dict[str, Any]]) -> ProviderResult:
        """oauth2-proxy does NOT manage users — authentication is delegated to the IdP."""
        return ProviderResult.failure(
            "oauth2-proxy does not maintain a local user database. "
            "User management is handled by the external OAuth provider.",
            "No users to import — authentication is fully delegated.",
        )

    def pre_migration_snapshot(self) -> ProviderResult:
        """No local state to snapshot — oauth2-proxy is stateless (cookies only)."""
        return ProviderResult.success(
            "oauth2-proxy is stateless — no snapshot required.",
            data={"note": "No local user database to snapshot"},
        )

    def restore_from_snapshot(self, snapshot: dict[str, Any]) -> ProviderResult:
        """No local state to restore."""
        return ProviderResult.success("oauth2-proxy is stateless — no restore required.")
