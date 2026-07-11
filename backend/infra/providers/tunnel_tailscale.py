"""backend/infra/providers/tunnel_tailscale.py

Tailscale tunnel provider.

Deploys the Tailscale container as the external access tunnel.
Uses tsnet/the Tailscale Docker container to expose services on the tailnet.
"""

from __future__ import annotations

import subprocess
from typing import Any, ClassVar

from backend.core import docker_client
from backend.core.compose import compose_up, write_fragment
from backend.core.config import config
from backend.core.state import StateDB
from backend.infra.base import InfraProvider, ProviderResult
from backend.infra.registry import register

CONTAINER_NAME = "tailscale"
IMAGE = "tailscale/tailscale:latest"  # last-verified: 2026-06-21 — upstream-tracking float (#1228)


@register
class TailscaleProvider(InfraProvider):
    slot = "tunnel"
    key = "tailscale"
    display_name = "Tailscale"

    # MANIFEST-LESS infra provider (no catalog/apps/tailscale.yaml) — `fields` is
    # hand-authored from this provider's own deploy() config knowledge
    # (auth_key/hostname/routes/exit_node). Judgment-class gap: an infra slot
    # provider with no catalog manifest has no manifest SSOT for its UI schema
    # (see #975 follow-up).
    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "auth_key",
            "label": "Tailscale auth key",
            "type": "text",
            "secret": True,
            "required": True,
            "help": "Reusable auth key from tailscale.com/admin/settings/keys.",
        },
        {
            "key": "hostname",
            "label": "Tailnet hostname",
            "type": "text",
            "placeholder": "slop",
            "required": False,
            "help": "Advertised hostname on the tailnet (default: slop).",
        },
        {
            "key": "routes",
            "label": "Advertised routes",
            "type": "text",
            "placeholder": "192.168.1.0/24",
            "required": False,
            "help": "Comma-separated subnets to advertise. Leave blank for none.",
        },
        {
            "key": "exit_node",
            "label": "Advertise as exit node",
            "type": "checkbox",
            "required": False,
            "help": "Allow this node to route tailnet traffic to the internet.",
        },
    ]
    category = "networking"

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        """Deploy the Tailscale container.

        Required config:
          auth_key — Tailscale auth key (reusable, from tailscale.com/admin/settings/keys)
        Optional:
          hostname — advertised hostname on the tailnet (default: slop)
          exit_node — advertise as an exit node
          routes — comma-separated subnets to advertise
        """
        with StateDB() as db:
            platform = db.get_platform()

        network = platform.network_name or "slop"
        auth_key = cfg.get("auth_key", "${TAILSCALE_AUTH_KEY}")
        hostname = cfg.get("hostname", "slop")
        advertise_routes = cfg.get("routes", "")
        exit_node = cfg.get("exit_node", False)

        env: dict[str, str] = {
            "TS_AUTHKEY": auth_key,
            "TS_HOSTNAME": hostname,
            "TS_STATE_DIR": "/var/lib/tailscale",
            "TS_USERSPACE": "false",
        }
        if advertise_routes:
            env["TS_ROUTES"] = advertise_routes
        if exit_node:
            env["TS_EXTRA_ARGS"] = "--advertise-exit-node"

        fragment = {
            "image": IMAGE,
            "container_name": CONTAINER_NAME,
            "restart": "unless-stopped",
            "networks": [network],
            "cap_add": ["NET_ADMIN", "SYS_MODULE"],
            "volumes": [
                f"{config.data_dir}/tailscale:/var/lib/tailscale",
                "/dev/net/tun:/dev/net/tun",
            ],
            "environment": env,
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure(
                    "Tailscale failed to start.",
                    detail=_out[:400],
                )
        except Exception as e:
            return ProviderResult.failure(f"Could not deploy Tailscale: {e}")

        with StateDB() as db:
            db.upsert_tunnel_provider(
                "tailscale",
                status="active",
                config={"hostname": hostname, "routes": advertise_routes},
            )

        # Register as a fully managed app — identical to catalog install.
        # This makes infra apps health-monitored, Dashboard-visible, with
        # operation history — exactly like apps installed from the Catalog.
        try:
            from backend.core.state import StateDB as _SDB2
            import time as _t2

            with _SDB2() as _db2:
                _db2.upsert_app(
                    "tailscale",
                    display_name=self.display_name,
                    tier=0,  # tier 0 = infrastructure layer
                    category=self.category,
                    status="running",
                    image=IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=None,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Tailscale deployed. Connect at https://{hostname}.tailnet-name.ts.net "
            f"(replace tailnet-name with your actual tailnet)."
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
            return ProviderResult.failure(f"Could not remove Tailscale: {e}")

        with StateDB() as db:
            db.remove_tunnel_provider("tailscale")
        return ProviderResult.success("Tailscale removed.")

    def verify(self) -> ProviderResult:
        c = docker_client.get_container(CONTAINER_NAME)
        if not c or c.status != "running":
            return ProviderResult.failure(
                "Tailscale container is not running.",
                detail="Run 'docker logs tailscale' to investigate.",
            )
        return ProviderResult.success("Tailscale is running.")

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        """Tailscale uses the tailnet hostname — individual app hostnames
        are handled by the reverse proxy (Traefik), not Tailscale itself."""
        return ProviderResult.success(
            f"Tailscale serves all apps via the tailnet hostname. "
            f"Traefik routes {hostname} → {target} internally."
        )

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        return ProviderResult.success("Tailscale hostname management is handled by Traefik.")

    def list_hostnames(self) -> ProviderResult:
        return ProviderResult.success("All apps accessible via tailnet.", data={})

    def pre_migration_snapshot(self) -> ProviderResult:
        return ProviderResult.success(
            "Tailscale tunnel state is external; no local snapshot required.", data={}
        )
