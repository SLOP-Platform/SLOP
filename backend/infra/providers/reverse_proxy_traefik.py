"""backend/infra/providers/reverse_proxy_traefik.py

Traefik reverse-proxy provider — the first provider of the FOUNDATIONAL
``reverse_proxy`` slot (#990).

Traefik is SLOP's edge router: every published app is reached through it, and the
auth slot's forwardAuth middleware is wired via Traefik labels. This provider makes
Traefik a first-class slot provider so the reverse proxy can eventually be swapped
(Caddy/nginx) the way auth/tunnel/etc. already can.

**Staged (#990 design, additive-first).** P1 (this module) wraps the EXISTING Traefik
deploy/label logic in ``backend.core.compose`` (``build_traefik_fragment`` /
``build_traefik_yaml`` / ``_traefik_labels``) — it changes nothing for the 86 catalog
apps (compose.py still emits their labels directly). P2 inverts that dependency so
``compose.py`` obtains route labels BY-REFERENCE through this provider's ``emit_*``
methods, making the provider the SOLE raw ``traefik.http.*`` emitter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import (
    STACK_NETWORK,
    build_traefik_fragment,
    build_traefik_yaml,
    compose_up,
    write_fragment,
)
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "traefik"


@register
class TraefikProvider(InfraProvider):
    slot = "reverse_proxy"
    key = "traefik"
    display_name = "Traefik"
    category = "reverse_proxy"

    # MANIFEST-LESS infra provider (no catalog/apps/traefik.yaml) — `fields` is
    # hand-authored from this provider's own deploy() config knowledge (the same
    # keys the setup wizard's Traefik steps consume).
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "domain",
            "label": "Public domain",
            "type": "text",
            "placeholder": "example.com",
            "required": True,
            "help": "Base domain — apps are published at <app>.<domain> with a wildcard cert.",
        },
        {
            "key": "cert_resolver",
            "label": "ACME cert resolver",
            "type": "text",
            "placeholder": "letsencrypt",
            "required": False,
            "help": "Which ACME resolver issues certs (letsencrypt | zerossl | buypass | staging).",
        },
        {
            "key": "dns_provider",
            "label": "DNS provider",
            "type": "text",
            "placeholder": "cloudflare",
            "required": False,
            "help": "DNS-01 challenge provider (cloudflare, route53, porkbun, …).",
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy (or restart) Traefik from the current platform config.

        Wraps the same builders the setup wizard uses (build_traefik_yaml +
        build_traefik_fragment), so this provider stays faithful to the canonical
        Traefik bring-up. Idempotent — safe to call on an already-running Traefik.
        """
        with StateDB() as db:
            platform = db.get_platform()

        domain = cfg.get("domain") or platform.domain or ""
        if not domain:
            return ProviderResult.failure(
                "Cannot deploy Traefik without a domain (set the platform domain first)."
            )
        network = cfg.get("network_name") or platform.network_name or STACK_NETWORK
        cert_resolver = cfg.get("cert_resolver") or platform.cert_resolver or "letsencrypt"
        dns_provider = cfg.get("dns_provider") or "cloudflare"
        config_root = str(cfg.get("config_root") or platform.config_root or config.data_dir)

        # Static config (traefik.yml) — DNS-01 wildcard, mirrors wizard step.
        traefik_yml = Path(config_root) / "traefik" / "traefik.yml"
        try:
            traefik_yml.parent.mkdir(parents=True, exist_ok=True)
            traefik_yml.write_text(
                build_traefik_yaml(
                    domain=domain,
                    cert_resolver=cert_resolver,
                    acme_email=cfg.get("acme_email") or f"admin@{domain}",
                    dns_provider=dns_provider,
                )
            )
        except OSError as e:
            return ProviderResult.failure(f"Could not write Traefik config: {e}")

        # Compose fragment + bring up.
        fragment = build_traefik_fragment(
            domain=domain,
            config_root=config_root,
            network_name=network,
            cert_resolver=cert_resolver,
            dns_provider=dns_provider,
        )
        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, out = compose_up(frag_path, pull=True, timeout=120)
            if rc != 0:
                return ProviderResult.failure("Traefik failed to start.", detail=out[:400])
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Traefik: {e}")

        with StateDB() as db:
            db.update_slot(
                "reverse_proxy",
                status="active",
                provider="traefik",
                config={"domain": domain, "cert_resolver": cert_resolver},
            )

        return ProviderResult.success(
            f"Traefik deployed. Apps published at *.{domain} (resolver: {cert_resolver})."
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
            return ProviderResult.failure(f"Could not remove Traefik: {e}")
        with StateDB() as db:
            db.update_slot("reverse_proxy", status="empty", provider=None, config={})
        return ProviderResult.success("Traefik removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure("Traefik is not running.")
        return ProviderResult.success("Traefik is running.")

    # ── reverse_proxy slot interface (the #990 emission seam) ──────────────────

    def emit_route_labels(
        self,
        key: str,
        domain: str,
        web_port: int | None,
        cert_resolver: str = "letsencrypt",
        lan_subnet: str | None = None,
        tinyauth_enabled: bool = False,
    ) -> list[str]:
        """Emit the Host(...)/router/service labels that publish an app via Traefik.

        #990 P2: this is now the SOLE raw per-app ``traefik.http.*`` route emitter —
        ``compose.build_service_fragment`` obtains an app's route labels BY-REFERENCE
        through the active reverse_proxy provider (this method), instead of a module-level
        ``compose._traefik_labels``. The body is the verbatim former emitter, so the
        emitted labels (incl. the tinyauth two-router forwardAuth pattern) are identical
        — a Caddy/nginx provider re-implements this facet differently. Pure (no I/O).
        """
        if not web_port:
            return []

        subdomain = f"{key}.{domain}"
        base = [
            "traefik.enable=true",
            f"traefik.http.services.{key}.loadbalancer.server.port={web_port}",
            # TLS wildcard cert
            f"traefik.http.routers.{key}.tls=true",
            f"traefik.http.routers.{key}.tls.certresolver={cert_resolver}",
            f"traefik.http.routers.{key}.tls.domains[0].main={domain}",
            f"traefik.http.routers.{key}.tls.domains[0].sans=*.{domain}",
        ]

        if tinyauth_enabled and lan_subnet:
            # Two-router pattern: LAN bypasses auth, internet hits Tinyauth
            base += [
                # LAN router (high priority, no auth)
                f"traefik.http.routers.{key}-lan.rule=Host(`{subdomain}`) && ClientIP(`{lan_subnet}`)",
                f"traefik.http.routers.{key}-lan.entrypoints=websecure",
                f"traefik.http.routers.{key}-lan.priority=10",
                f"traefik.http.routers.{key}-lan.tls=true",
                f"traefik.http.routers.{key}-lan.tls.certresolver={cert_resolver}",
                f"traefik.http.routers.{key}-lan.service={key}",
                # Catch-all router (lower priority, Tinyauth middleware)
                f"traefik.http.routers.{key}.rule=Host(`{subdomain}`)",
                f"traefik.http.routers.{key}.entrypoints=websecure",
                f"traefik.http.routers.{key}.priority=5",
                f"traefik.http.routers.{key}.middlewares=tinyauth-auth@docker",
                f"traefik.http.routers.{key}.service={key}",
            ]
        else:
            base += [
                f"traefik.http.routers.{key}.rule=Host(`{subdomain}`)",
                f"traefik.http.routers.{key}.entrypoints=websecure",
                f"traefik.http.routers.{key}.service={key}",
            ]

        return base

    def emit_forwardauth_labels(
        self, key: str, middleware: str = "tinyauth-auth@docker"
    ) -> list[str]:
        """Emit the per-app label wiring it to the active auth slot's forwardAuth
        middleware. The middleware itself is defined by the auth provider; this is the
        router-side reference (mirrors compose.py::_traefik_labels' tinyauth branch)."""
        if not middleware:
            return []
        return [f"traefik.http.routers.{key}.middlewares={middleware}"]
