"""Auto-import all infrastructure providers.
Each provider module uses the @register decorator from backend.infra.registry.
Importing this package makes all providers available to list_providers().
"""

# These imports trigger @register calls in each module.
# Order is display order in the UI.
from backend.infra.providers.auth_tinyauth import TinyauthProvider
from backend.infra.providers.tunnel_cloudflare import CloudflareTunnelProvider
from backend.infra.providers.tunnel_tailscale import TailscaleProvider
from backend.infra.providers.vpn_gluetun import GluetunProvider
from backend.infra.providers.dashboard_homepage import HomepageProvider
from backend.infra.providers.dashboard_glance import GlanceDashboardProvider
from backend.infra.providers.management_portainer import PortainerProvider
from backend.infra.providers.management_alternatives import (
    DockhandProvider,
    DockgeProvider,
    KomodoProvider,
    PortainerBEProvider,
)

__all__ = [
    "CloudflareTunnelProvider",
    "DockgeProvider",
    "DockhandProvider",
    "GlanceDashboardProvider",
    "GluetunProvider",
    "HomepageProvider",
    "KomodoProvider",
    "PortainerBEProvider",
    "PortainerProvider",
    "TailscaleProvider",
    "TinyauthProvider",
]
