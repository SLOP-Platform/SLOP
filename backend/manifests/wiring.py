"""backend/manifests/wiring.py

Indexer and download-client wire_type implementations.

SLOP's wiring model (CLAUDE.md mantra: Deploy ≠ Configure ≠ Healthy):
a wiring row is written at install time with status='pending'. The ACTUAL
configuration happens here, asynchronously, and is retried by the health
scheduler on each cycle until it succeeds. Nothing blocks the install on
these side-effects.

Wire flow for wire_type == "indexer":
  1. Read Prowlarr's API key from {config_root}/prowlarr/config.xml.
  2. Read the source arr app's API key from {config_root}/{source_key}/config.xml.
  3. If either config.xml is absent, the app has not finished first-boot init —
     return "deferred" so the scheduler retries later.
  4. Check Prowlarr's existing applications (idempotent): if the source app's
     base URL is already registered, return "wired" without re-POSTing.
  5. POST a new Prowlarr "application" so Prowlarr syncs its indexers to the arr.

Wire flow for wire_type == "download_client":
  1. Read SABnzbd's API key from {config_root}/sabnzbd/sabnzbd.ini.
  2. Read the target arr app's API key from {config_root}/{target_key}/config.xml.
  3. If either config file is absent or the key is unreadable, return "deferred".
  4. Check the arr app's existing download clients (idempotent): if SABnzbd is
     already registered, return "wired" without re-POSTing.
  5. POST a new download client entry to the arr app's /api/v3/downloadclient.

All HTTP errors fail open on the *check* (so we retry the POST) but the POST
itself returns "failed" on a non-2xx so the row is marked failed and surfaced.
"""

from __future__ import annotations

import configparser
import xml.etree.ElementTree as ET
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx

from backend.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Wire-type handler registry
# ---------------------------------------------------------------------------
# Maps wire_type strings to their implementation functions.
# Handler signature:
#   (source_key: str, source_manifest: Any, config_root: str,
#    target_key: str | None = None) -> str
# Return values: "wired" | "deferred" | "failed"
#
# target_key is forwarded by the dispatcher so handlers that need to know
# which app to configure (e.g. download_client) receive it.  Handlers that
# don't use it must accept it as an ignored keyword argument.
#
# To add a new wire_type:
#   1. Implement a handler function in this module with the signature above.
#   2. Register it: WIRE_HANDLERS["your_type"] = your_handler
#
# The registry is populated after all handlers are defined (at module bottom).
WIRE_HANDLERS: dict[str, Callable[..., str]] = {}

# Internal Docker-network URL for Prowlarr (containers resolve by service name).
PROWLARR_URL = "http://prowlarr:9696"

_HTTP_TIMEOUT = 5.0

# Container-internal web port for each arr app (the port Prowlarr talks to over
# the slop network, NOT the remapped host port). Falls back to the manifest's
# web_port when an app is not in this map.
_APP_PORT_MAP: dict[str, int] = {
    "sonarr": 8989,
    "radarr": 7878,
    "lidarr": 8686,
    "readarr": 8787,
    "whisparr": 6969,
    "mylar3": 8090,  # container-internal port; host port 8091 is the remapped value
}

# Prowlarr "application" implementation metadata per arr app:
# (name, implementationName, implementation, configContract).
_APP_CONFIG_MAP: dict[str, tuple[str, str, str, str]] = {
    "sonarr": ("Sonarr", "Sonarr", "Sonarr", "SonarrSettings"),
    "radarr": ("Radarr", "Radarr", "Radarr", "RadarrSettings"),
    "lidarr": ("Lidarr", "Lidarr", "Lidarr", "LidarrSettings"),
    "readarr": ("Readarr", "Readarr", "Readarr", "ReadarrSettings"),
    "whisparr": ("Whisparr", "Whisparr", "Whisparr", "WhisparrSettings"),
    "mylar3": ("Mylar3", "Mylar3", "Mylar3", "Mylar3Settings"),
}

# syncCategories differ by app family. Sonarr = TV (5xxx), Radarr = Movies
# (2xxx), Lidarr = Music (3xxx), Readarr = Books (7xxx/8xxx), Whisparr = XXX
# (6xxx), Mylar3 = Comics (7030). These mirror the *arr default category
# groupings Prowlarr ships with.
_SYNC_CATEGORIES: dict[str, list[int]] = {
    "sonarr": [5000, 5010, 5020, 5030, 5040, 5045, 5050],
    "radarr": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060],
    "lidarr": [3000, 3010, 3020, 3030, 3040],
    "readarr": [3030, 7000, 7010, 7020, 7030, 8000, 8010],
    "whisparr": [6000, 6010, 6020, 6030, 6040, 6045, 6050, 6060, 6070, 6080, 6090],
    "mylar3": [7030],  # Comics/Manga — Prowlarr category 7030
}


# ---------------------------------------------------------------------------
# Download-client metadata — per arr app
# ---------------------------------------------------------------------------
# Each arr app uses a different implementation/configContract pair when adding
# a download client. The tuple is (implementationName, implementation,
# configContract) — mirroring the Prowlarr application map above.
_DC_CONFIG_MAP: dict[str, tuple[str, str, str]] = {
    "sonarr": ("SABnzbd", "Sabnzbd", "SabnzbdSettings"),
    "radarr": ("SABnzbd", "Sabnzbd", "SabnzbdSettings"),
    "lidarr": ("SABnzbd", "Sabnzbd", "SabnzbdSettings"),
    "readarr": ("SABnzbd", "Sabnzbd", "SabnzbdSettings"),
    "whisparr": ("SABnzbd", "Sabnzbd", "SabnzbdSettings"),
}

# Container-internal port map for each arr app (same as _APP_PORT_MAP but
# referenced in the download-client context so arr-app URLs are resolved).
# The POST target is the arr-app API, not SABnzbd.
_ARR_PORT_MAP: dict[str, int] = {
    "sonarr": 8989,
    "radarr": 7878,
    "lidarr": 8686,
    "readarr": 8787,
    "whisparr": 6969,
}


def _read_sabnzbd_api_key(config_root: str) -> str | None:
    """Return SABnzbd's api_key from ``{config_root}/sabnzbd/sabnzbd.ini``.

    SABnzbd stores its API key under ``[misc]`` → ``api_key`` in an INI-style
    config file (not an XML config.xml like the arr apps).

    Returns None when the file is missing, unparseable, or the key is empty —
    caller treats None as "defer and retry".
    """
    import configparser

    path = Path(config_root) / "sabnzbd" / "sabnzbd.ini"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        log.warning("wiring: could not parse %s: %s", path, exc)
        return None
    value = parser.get("misc", "api_key", fallback="").strip()
    return value or None


def _read_api_key(app_key: str, config_root: str) -> str | None:
    """Return the API key for an app from its config file.

    Servarr apps (sonarr/radarr/etc.) store their key in config.xml as <ApiKey>.
    Mylar3 uses an INI-style config.ini with api_key under [Interface].

    Returns None when the file is missing (app not yet first-booted) or the
    key cannot be found / parsed — caller treats None as "defer and retry".
    """
    if app_key == "mylar3":
        return _read_mylar3_api_key(config_root)
    path = Path(config_root) / app_key / "config.xml"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None
    try:
        root = ET.fromstring(text)  # noqa: S314  # nosec B314  # config.xml is operator-controlled, not untrusted input
    except ET.ParseError as exc:
        log.warning("wiring: could not parse %s: %s", path, exc)
        return None
    elem = root.find("ApiKey")
    if elem is None:
        return None
    value = (elem.text or "").strip()
    return value or None


def _read_mylar3_api_key(config_root: str) -> str | None:
    """Read Mylar3's API key from {config_root}/mylar3/config.ini.

    Mylar3 uses an INI config (not XML). The key is at [Interface] api_key.
    Returns None when the file is missing or the key is not yet written.
    """
    path = Path(config_root) / "mylar3" / "config.ini"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text)
    except configparser.Error as exc:
        log.warning("wiring: could not parse mylar3 config.ini: %s", exc)
        return None
    value = cfg.get("Interface", "api_key", fallback="").strip()
    return value or None


def _source_url(source_key: str, port: int) -> str:
    """Internal slop-network URL for an arr app: ``http://{source_key}:{port}``."""
    return f"http://{source_key}:{port}"


def _is_registered(source_url: str, prowlarr_url: str, prowlarr_api_key: str) -> bool:
    """True when Prowlarr already has an application whose baseUrl == source_url.

    Fails OPEN (returns False) on any HTTP/timeout/parse error so the caller
    proceeds to POST and we retry the registration rather than silently skipping.
    """
    try:
        resp = httpx.get(
            f"{prowlarr_url}/api/v1/applications",
            headers={"X-Api-Key": prowlarr_api_key},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning(
                "wiring: GET applications returned %d; treating as not-registered",
                resp.status_code,
            )
            return False
        apps = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("wiring: could not read Prowlarr applications: %s", exc)
        return False

    if not isinstance(apps, list):
        return False
    for app in apps:
        for field in app.get("fields") or []:
            if field.get("name") == "baseUrl" and field.get("value") == source_url:
                return True
    return False


def _post_application(
    source_key: str,
    source_url: str,
    source_api_key: str,
    prowlarr_url: str,
    prowlarr_api_key: str,
) -> bool:
    """POST a new Prowlarr application for ``source_key``. True iff 201 Created."""
    meta = _APP_CONFIG_MAP.get(source_key)
    if meta is None:
        log.error("wiring: no Prowlarr application mapping for '%s'", source_key)
        return False
    name, implementation_name, implementation, config_contract = meta

    body: dict[str, Any] = {
        "syncLevel": "fullSync",
        "name": name,
        "tags": [],
        "fields": [
            {"name": "prowlarrUrl", "value": prowlarr_url},
            {"name": "baseUrl", "value": source_url},
            {"name": "apiKey", "value": source_api_key},
            {"name": "syncCategories", "value": _SYNC_CATEGORIES.get(source_key, [])},
            {"name": "animeSyncCategories", "value": []},
            {"name": "syncAnimeStandardFormat", "value": False},
        ],
        "implementationName": implementation_name,
        "implementation": implementation,
        "configContract": config_contract,
    }
    try:
        resp = httpx.post(
            f"{prowlarr_url}/api/v1/applications",
            headers={
                "X-Api-Key": prowlarr_api_key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.error("wiring: POST application for '%s' failed: %s", source_key, exc)
        return False

    if resp.status_code == 201:
        log.info("wiring: registered '%s' in Prowlarr (%s)", source_key, source_url)
        return True

    log.error(
        "wiring: POST application for '%s' returned %d: %s",
        source_key,
        resp.status_code,
        resp.text[:500],
    )
    return False


def _resolve_port(source_key: str, source_manifest: Any) -> int:
    """Container-internal web port for the arr app.

    Prefers the well-known port map; falls back to the manifest's ``web_port``
    (the loader exposes ``web_port`` directly) and finally to 0 if unknown.
    """
    if source_key in _APP_PORT_MAP:
        return _APP_PORT_MAP[source_key]
    web_port = getattr(source_manifest, "web_port", None)
    if web_port is None:
        # Defensive: some callers/tests pass a manifest exposing ports.web.
        ports = getattr(source_manifest, "ports", None)
        web_port = getattr(ports, "web", None) if ports is not None else None
    return int(web_port) if web_port else 0


def wire_indexer(
    source_key: str,
    source_manifest: Any,
    config_root: str,
    target_key: str | None = None,  # accepted for dispatcher uniformity; unused by this handler
) -> str:
    """Register ``source_key`` (an arr app) in Prowlarr as an indexer consumer.

    Returns one of:
      "wired"    — registration confirmed (already present, or POST succeeded).
      "deferred" — a prerequisite isn't ready yet (config.xml/API key missing);
                   the health scheduler should retry on a later cycle.
      "failed"   — Prowlarr rejected the registration (non-201). Surfaced to UI.
    """
    prowlarr_api_key = _read_api_key("prowlarr", config_root)
    if not prowlarr_api_key:
        log.info("wiring: Prowlarr API key not ready — deferring '%s'", source_key)
        return "deferred"

    source_api_key = _read_api_key(source_key, config_root)
    if not source_api_key:
        log.info("wiring: '%s' API key not ready — deferring", source_key)
        return "deferred"

    port = _resolve_port(source_key, source_manifest)
    if not port:
        log.error("wiring: could not resolve port for '%s' — failing", source_key)
        return "failed"
    source_url = _source_url(source_key, port)

    if _is_registered(source_url, PROWLARR_URL, prowlarr_api_key):
        log.info("wiring: '%s' already registered in Prowlarr", source_key)
        return "wired"

    if _post_application(
        source_key,
        source_url,
        source_api_key,
        PROWLARR_URL,
        prowlarr_api_key,
    ):
        return "wired"
    return "failed"


def _is_download_client_registered(
    target_url: str,
    target_api_key: str,
    client_name: str,
) -> bool:
    """True when the arr app already has a download client named ``client_name``.

    Fails OPEN (returns False) on any HTTP/timeout/parse error so the caller
    proceeds to POST and retries the registration rather than silently skipping.
    """
    try:
        resp = httpx.get(
            f"{target_url}/api/v3/downloadclient",
            headers={"X-Api-Key": target_api_key},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            log.warning(
                "wiring: GET downloadclient returned %d; treating as not-registered",
                resp.status_code,
            )
            return False
        clients = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("wiring: could not read download clients from %s: %s", target_url, exc)
        return False

    if not isinstance(clients, list):
        return False
    for client in clients:
        if client.get("name") == client_name:
            return True
    return False


def _post_download_client(
    target_key: str,
    target_url: str,
    target_api_key: str,
    sabnzbd_api_key: str,
) -> bool:
    """POST a SABnzbd download client entry to the arr app at ``target_url``.

    True iff the response status is 200 or 201.
    """
    meta = _DC_CONFIG_MAP.get(target_key)
    if meta is None:
        log.error(
            "wiring: no download-client config mapping for arr app '%s'",
            target_key,
        )
        return False
    implementation_name, implementation, config_contract = meta

    body: dict[str, Any] = {
        "enable": True,
        "protocol": "usenet",
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "name": "SABnzbd",
        "fields": [
            {"name": "host", "value": "sabnzbd"},
            {"name": "port", "value": 8080},
            {"name": "apiKey", "value": sabnzbd_api_key},
            {"name": "tvCategory", "value": "tv"},
            {"name": "recentTvPriority", "value": -100},
            {"name": "olderTvPriority", "value": -100},
            {"name": "useSsl", "value": False},
        ],
        "implementationName": implementation_name,
        "implementation": implementation,
        "configContract": config_contract,
        "tags": [],
    }
    try:
        resp = httpx.post(
            f"{target_url}/api/v3/downloadclient",
            headers={
                "X-Api-Key": target_api_key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.error(
            "wiring: POST download client to '%s' failed: %s",
            target_key,
            exc,
        )
        return False

    if resp.status_code in (200, 201):
        log.info(
            "wiring: SABnzbd registered as download client in '%s' (%s)",
            target_key,
            target_url,
        )
        return True

    log.error(
        "wiring: POST download client to '%s' returned %d: %s",
        target_key,
        resp.status_code,
        resp.text[:500],
    )
    return False


def wire_download_client(
    source_key: str,
    source_manifest: Any,
    config_root: str,
    target_key: str | None = None,
) -> str:
    """Configure the target arr app to use SABnzbd as its download client.

    ``source_key`` is always "sabnzbd" (the download-client app).
    ``target_key`` is the arr app to configure (sonarr, radarr, lidarr, readarr,
    whisparr).  When ``target_key`` is None the handler returns "failed" — the
    dispatcher must supply the target.

    Returns one of:
      "wired"    — SABnzbd is confirmed as a download client in the arr app
                   (already present, or POST succeeded).
      "deferred" — a prerequisite isn't ready yet (config file / API key missing);
                   the health scheduler should retry on a later cycle.
      "failed"   — the arr app rejected the registration.  Surfaced to UI.
    """
    if not target_key:
        log.error(
            "wiring: wire_download_client called without target_key for '%s' — failing",
            source_key,
        )
        return "failed"

    sabnzbd_api_key = _read_sabnzbd_api_key(config_root)
    if not sabnzbd_api_key:
        log.info(
            "wiring: SABnzbd API key not ready — deferring download_client wire for '%s'",
            target_key,
        )
        return "deferred"

    target_api_key = _read_api_key(target_key, config_root)
    if not target_api_key:
        log.info(
            "wiring: '%s' API key not ready — deferring download_client wire",
            target_key,
        )
        return "deferred"

    port = _ARR_PORT_MAP.get(target_key)
    if not port:
        log.error(
            "wiring: no port mapping for arr app '%s' — failing download_client wire",
            target_key,
        )
        return "failed"
    target_url = _source_url(target_key, port)

    if _is_download_client_registered(target_url, target_api_key, "SABnzbd"):
        log.info(
            "wiring: SABnzbd already registered as download client in '%s'",
            target_key,
        )
        return "wired"

    if _post_download_client(
        target_key,
        target_url,
        target_api_key,
        sabnzbd_api_key,
    ):
        return "wired"
    return "failed"


# Populate the registry after wire_indexer is defined.
WIRE_HANDLERS["indexer"] = wire_indexer
WIRE_HANDLERS["download_client"] = wire_download_client
