from typing import Any

"""backend/api/infra_schemas.py

Provider configuration schemas — tells the UI what fields each provider
needs when deploying or swapping an infrastructure slot.

Extracted from infra.py for maintainability. Each entry is a list of
field descriptors consumed by the InfraView deploy modals.
"""

PROVIDER_CONFIG_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    # ── Auth ──────────────────────────────────────────────────────────────
    "tinyauth": [
        {
            "key": "lan_subnet",
            "label": "LAN subnet (optional)",
            "placeholder": "10.0.0.0/8",
            "required": False,
            "secret": False,
            "help": "Requests from this subnet bypass authentication. Leave blank to require auth everywhere.",
        },
    ],
    "authelia": [
        {
            "key": "domain",
            "label": "Base domain",
            "placeholder": "example.com",
            "required": True,
            "secret": False,
            "help": "Your domain — Authelia will be available at auth.example.com",
        },
        {
            "key": "jwt_secret",
            "label": "JWT secret",
            "placeholder": "generate a random 32+ char string",
            "required": True,
            "secret": True,
            "help": "Used to sign session tokens. Use a random string — keep it secret.",
        },
        {
            "key": "session_secret",
            "label": "Session secret",
            "placeholder": "generate a random 32+ char string",
            "required": True,
            "secret": True,
            "help": "Encrypts session cookies. Different from JWT secret.",
        },
    ],
    # ── Tunnel ────────────────────────────────────────────────────────────
    "cloudflared": [
        {
            "key": "tunnel_token",
            "label": "Tunnel token",
            "placeholder": "eyJhIjoiXX...",
            "required": True,
            "secret": True,
            "help": "From Cloudflare Zero Trust → Networks → Tunnels → Create tunnel → copy token.",
        },
        {
            "key": "auto_register",
            "label": "Auto-register hostnames on app install",
            "type": "checkbox",
            "default": True,
            "required": False,
            "secret": False,
            "help": "Automatically add CF Tunnel ingress rules when apps are installed.",
        },
    ],
    "headscale": [
        {
            "key": "server_url",
            "label": "Headscale server URL",
            "placeholder": "https://headscale.example.com",
            "required": True,
            "secret": False,
            "help": "URL of your self-hosted Headscale server (must be publicly accessible).",
        },
        {
            "key": "pre_auth_key",
            "label": "Pre-auth key",
            "placeholder": "from headscale preauthkeys create",
            "required": True,
            "secret": True,
            "help": "Generate with: headscale preauthkeys create --reusable --expiration 720h",
        },
        {
            "key": "hostname",
            "label": "Node hostname (optional)",
            "placeholder": "slop",
            "required": False,
            "secret": False,
            "help": "How this node appears in your Headscale network. Defaults to 'slop'.",
        },
    ],
    "tailscale": [
        {
            "key": "auth_key",
            "label": "Auth key",
            "placeholder": "tskey-auth-...",
            "required": True,
            "secret": True,
            "help": "From tailscale.com → Settings → Keys → Generate auth key. Use a reusable key.",
        },
        {
            "key": "hostname",
            "label": "Tailnet hostname (optional)",
            "placeholder": "slop",
            "required": False,
            "secret": False,
            "help": "How this server appears on your tailnet. Defaults to 'slop'.",
        },
        {
            "key": "routes",
            "label": "Advertise routes (optional)",
            "placeholder": "10.0.1.0/24",
            "required": False,
            "secret": False,
            "help": "Comma-separated subnets to advertise to your tailnet (subnet routing).",
        },
    ],
    # ── Dashboard ─────────────────────────────────────────────────────────
    "homepage": [
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "3000",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 3000 if something else is using it.",
        },
    ],
    "glance": [
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "8080",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 8080.",
        },
    ],
    # ── Management ────────────────────────────────────────────────────────
    "portainer": [
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "9000",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 9000.",
        },
    ],
    "portainer_be": [
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "9000",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 9000.",
        },
        {
            "key": "_license_note",
            "label": "",
            "type": "info",
            "required": False,
            "secret": False,
            "help": "After deploy, activate your Business Edition license at Settings → Licenses in the Portainer UI. The license key is NOT entered here.",
        },
    ],
    "dockhand": [
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "3000",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 3000.",
        },
        {
            "key": "use_postgres",
            "label": "Use shared PostgreSQL (optional)",
            "type": "checkbox",
            "default": False,
            "required": False,
            "secret": False,
            "help": "Use the SLOP managed PostgreSQL instead of SQLite. Recommended for multi-user setups.",
        },
    ],
    "dockge": [
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "5001",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 5001.",
        },
        {
            "key": "stacks_dir",
            "label": "Stacks directory",
            "placeholder": "/opt/stacks",
            "required": False,
            "secret": False,
            "help": "IMPORTANT: This path must be identical inside and outside the container.",
        },
    ],
    "komodo": [
        {
            "key": "jwt_secret",
            "label": "JWT secret",
            "placeholder": "generate a random 32+ char string",
            "required": True,
            "secret": True,
            "help": "Random secret for signing tokens. Keep it private.",
        },
        {
            "key": "passkey",
            "label": "Core↔Periphery passkey",
            "placeholder": "generate a random 32+ char string",
            "required": True,
            "secret": True,
            "help": "Authenticates the Periphery agent to Komodo Core. Keep it private.",
        },
        {
            "key": "port",
            "label": "Host port (optional)",
            "placeholder": "9120",
            "required": False,
            "type": "number",
            "secret": False,
            "help": "Override the default port 9120.",
        },
    ],
    "gluetun": [
        {
            "key": "vpn_provider",
            "label": "VPN provider",
            "placeholder": "mullvad",
            "required": True,
            "secret": False,
            "help": "Supported: mullvad, nordvpn, expressvpn, surfshark, protonvpn, pia, airvpn, and more.",
        },
        {
            "key": "wireguard_private_key",
            "label": "WireGuard private key",
            "placeholder": "from your VPN provider dashboard",
            "required": True,
            "secret": True,
            "help": "WireGuard private key from your VPN provider's config download.",
        },
        {
            "key": "wireguard_addresses",
            "label": "WireGuard addresses",
            "placeholder": "10.64.0.1/32",
            "required": False,
            "secret": False,
            "help": "WireGuard interface addresses (from VPN config). Required for some providers.",
        },
        {
            "key": "server_countries",
            "label": "Server countries (optional)",
            "placeholder": "Netherlands,Sweden",
            "required": False,
            "secret": False,
            "help": "Comma-separated country names to filter server selection.",
        },
    ],
}
