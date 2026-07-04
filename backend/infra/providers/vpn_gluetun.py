"""backend/infra/providers/vpn_gluetun.py

Gluetun VPN provider — routes download client traffic through a VPN.

Gluetun is a VPN client in a container supporting 40+ providers.
Apps that need VPN access use: network_mode: service:gluetun
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

CONTAINER_NAME = "gluetun"
IMAGE = "qmcgaw/gluetun"


@register
class GluetunProvider(InfraProvider):
    slot = "vpn"
    key = "gluetun"
    display_name = "Gluetun"
    description = (
        "VPN client container supporting 40+ providers (Mullvad, Surfshark, NordVPN, etc.)"
    )

    fields: ClassVar[list[dict[str, Any]]] = [
        {
            "key": "vpn_service_provider",
            "label": "VPN Provider",
            "type": "text",
            "placeholder": "mullvad",
            "required": True,
            "help": "Provider name: mullvad, surfshark, nordvpn, expressvpn, protonvpn, pia, etc.",
        },
        {
            "key": "openvpn_user",
            "label": "Username / Account number",
            "type": "text",
            "required": False,
            "help": "For Mullvad: your account number. For others: your username.",
        },
        {
            "key": "openvpn_password",
            "label": "Password",
            "type": "text",
            "secret": True,
            "required": False,
            "help": "Not required for Mullvad (leave blank).",
        },
        {
            "key": "wireguard_private_key",
            "label": "WireGuard Private Key",
            "type": "text",
            "secret": True,
            "required": False,
            "help": "Required for providers using WireGuard protocol.",
        },
        {
            "key": "server_countries",
            "label": "Server countries",
            "type": "text",
            "placeholder": "Netherlands",
            "required": False,
            "help": "Comma-separated country names. Leave blank for automatic selection.",
        },
        {
            "type": "info",
            "key": "_info",
            "label": "",
            "help": (
                "After deployment, route download clients through VPN by setting "
                "network_mode: service:gluetun in their compose fragment. "
                "Gluetun exposes a proxy on port 8888."
            ),
        },
    ]

    def deploy(self, cfg: dict[str, Any]) -> ProviderResult:
        vpn_provider = cfg.get("vpn_service_provider", "").strip()
        if not vpn_provider:
            return ProviderResult.failure(
                "VPN provider name is required.",
                "Set 'VPN Provider' to your provider (e.g. mullvad, nordvpn).",
            )

        # Determine VPN type: wireguard if key present, else use cfg, else openvpn
        vpn_type = "openvpn"
        if cfg.get("wireguard_private_key"):
            vpn_type = "wireguard"
        elif cfg.get("vpn_type"):
            vpn_type = cfg["vpn_type"].lower()

        env: dict[str, str] = {
            "VPN_SERVICE_PROVIDER": vpn_provider,
            "VPN_TYPE": vpn_type,
        }

        if cfg.get("openvpn_user"):
            env["OPENVPN_USER"] = cfg["openvpn_user"]
        if cfg.get("openvpn_password"):
            env["OPENVPN_PASSWORD"] = cfg["openvpn_password"]
        if cfg.get("wireguard_private_key"):
            env["WIREGUARD_PRIVATE_KEY"] = cfg["wireguard_private_key"]
        if cfg.get("server_countries"):
            env["SERVER_COUNTRIES"] = cfg["server_countries"]

        fragment = {
            "image": "qmcgaw/gluetun:latest",
            "container_name": CONTAINER_NAME,
            "cap_add": ["NET_ADMIN"],
            "devices": ["/dev/net/tun:/dev/net/tun"],
            "environment": env,
            "ports": [
                "8888:8888/tcp",  # HTTP proxy
                "8388:8388/tcp",  # Shadowsocks
                "8388:8388/udp",
            ],
            "restart": "unless-stopped",
        }

        try:
            frag_path = write_fragment(CONTAINER_NAME, fragment)
            rc, _out = compose_up(frag_path, timeout=90)
            if rc != 0:
                return ProviderResult.failure(
                    "Gluetun failed to start.",
                    _out[:400],
                )
        except Exception as e:
            return ProviderResult.failure("Could not deploy Gluetun.", str(e))

        with StateDB() as db:
            db.update_slot(
                "vpn",
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
                    "gluetun",
                    display_name="Gluetun VPN",
                    tier=0,  # tier 0 = infrastructure layer
                    category="networking",
                    status="running",
                    image=IMAGE,
                    image_tag="latest",
                    container_name=CONTAINER_NAME,
                    host_port=8888,
                    last_healthy_at=int(_t2.time()),
                )
        except Exception as _e2:
            import logging as _l2

            _l2.getLogger(__name__).debug("Could not register infra app in DB: %s", _e2)

        return ProviderResult.success(
            f"Gluetun ({vpn_provider}) deployed. "
            "Route apps through VPN by setting network_mode: service:gluetun.",
        )

    def remove(self) -> ProviderResult:
        try:
            frag_path = config.compose_dir / f"{CONTAINER_NAME}.yaml"
            if frag_path.exists():
                rc, _out = compose_down(frag_path)
                if rc != 0:
                    log.warning("Gluetun stop failed: %s", _out[:200])
            frag_path.unlink(missing_ok=True)
        except Exception as e:
            return ProviderResult.failure("Could not remove Gluetun.", str(e))

        with StateDB() as db:
            db.update_slot(
                "vpn", provider=None, status="empty", display_name=None, container_name=None
            )

        return ProviderResult.success("Gluetun removed.")

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
                return ProviderResult.success("Gluetun is running.")
            return ProviderResult.failure(f"Gluetun status: {status or 'not found'}.", "")
        except Exception as e:
            return ProviderResult.failure("Cannot check Gluetun.", str(e))
