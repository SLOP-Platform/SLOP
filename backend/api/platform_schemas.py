"""backend/api/platform_schemas.py

Wizard request schema, extracted from platform.py (#1302 linecount drain).

``WizardRequest`` is the large (validated) request body for the setup wizard's
final apply call. Its field validators are self-contained (lazy imports); the
network-name regex it uses moves with it. platform.py re-imports it so the route
body annotation and `from backend.api.platform import WizardRequest` (tests)
resolve unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.core.logging import get_logger

log = get_logger(__name__)

_NETWORK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class WizardRequest(BaseModel):
    domain: str = Field(..., description="Base domain e.g. example.com")
    secrets: dict[str, str] = Field(
        default_factory=dict, description="Secrets from Stage 5: API tokens, generated passwords"
    )
    eab_kid: str = Field("", description="ZeroSSL EAB Key ID")
    eab_hmac: str = Field("", description="ZeroSSL EAB HMAC Key")
    ntfy_url: str = Field("http://ntfy:80", description="ntfy server URL")
    ntfy_topic: str = Field("slop", description="ntfy topic")
    ntfy_enabled: bool = Field(False, description="Enable ntfy notifications")
    infra_selections: dict[str, Any] = Field(
        default_factory=dict,
        description="Infra slot selections from Stage 3 — values may be str or list[str] (tunnel is multi-select)",
    )
    selected_stacks: list[str] = Field(
        default_factory=list, description="Quick stack IDs from Stage 4"
    )
    llm_provider: str = Field("ollama", description="LLM provider: ollama|groq|cerebras|openai|awan|llamacpp")
    groq_api_key: str = Field("", description="Groq API key if provider=groq")
    config_root: str = Field(
        "/var/lib/slop/config",
        description="Absolute path for app config folders",
    )
    media_root: str = Field(
        "/mnt/media",
        description="Absolute path for media library",
    )
    puid: int = Field(
        1000,
        ge=1,
        le=65534,
        description="File owner UID for linuxserver containers (must not be 0/root)",
    )
    pgid: int = Field(
        1000,
        ge=1,
        le=65534,
        description="File owner GID for linuxserver containers (must not be 0/root)",
    )
    timezone: str = Field("America/Los_Angeles", description="TZ database name")
    cert_resolver: str = Field("letsencrypt", description="Traefik cert resolver name")
    network_name: str = Field("slop", description="Docker network name")
    # TLS / DNS-01 settings
    acme_email: str = Field(
        "", description="Email for Let's Encrypt account (defaults to admin@domain)"
    )

    @field_validator("dns_provider")
    @classmethod
    def validate_dns_provider(cls, v: str) -> str:
        from backend.core.compose import _PROVIDER_ENV_VARS

        if v and v not in _PROVIDER_ENV_VARS:
            known = ", ".join(sorted(_PROVIDER_ENV_VARS.keys()))
            raise ValueError(f"Unknown DNS provider '{v}'. Supported providers: {known}")
        return v or "cloudflare"

    dns_provider: str = Field(
        "cloudflare",
        description=(
            "DNS provider for ACME DNS-01 challenge. Required for wildcard certs. "
            "Options: cloudflare, route53, namecheap, porkbun, digitalocean, gandi, "
            "hetzner, linode, ovh, godaddy, duckdns, google, azure, desec, and 80+ more. "
            "Full list: https://doc.traefik.io/traefik/https/acme/#providers"
        ),
    )
    include_zerossl: bool = Field(
        False,
        description="Include ZeroSSL resolver (only needed when cert_resolver=zerossl)",
    )

    @field_validator("domain")
    @classmethod
    def domain_must_have_dot(cls, v: str) -> str:
        v = v.strip().lower()
        if "\n" in v or "\r" in v:
            raise ValueError("domain must not contain newlines")
        if "." not in v:
            raise ValueError("Domain must contain at least one dot, e.g. 'example.com'")
        return v

    @field_validator("config_root", "media_root")
    @classmethod
    def must_be_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("Path must be absolute (start with '/')")
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        if not v:
            raise ValueError("Timezone is required. Example: 'America/Los_Angeles'.")
        try:
            from zoneinfo import available_timezones

            if v not in available_timezones():
                raise ValueError(
                    f"'{v}' is not a valid IANA timezone name. "
                    "Example: 'America/Los_Angeles'. "
                    "Use the timezone dropdown to pick a valid zone."
                )
        except ImportError:
            # zoneinfo is available in Python ≥ 3.9 (stdlib). If missing, the
            # runtime is unexpectedly old — log at WARNING so it isn't invisible.
            log.warning(
                "zoneinfo not available — skipping IANA timezone validation in Pydantic model. "
                "Upgrade to Python 3.9+ to enable this check."
            )
        return v

    # ── Security validators ────────────────────────────────────────────────
    # C-1 / C-2: EAB fields — base64url charset only, no newlines or YAML chars
    @field_validator("eab_kid", "eab_hmac")
    @classmethod
    def no_injection_chars(cls, v: str) -> str:
        if v and not re.match(r"^[A-Za-z0-9\-_=]+$", v):
            raise ValueError(
                "Invalid characters — EAB fields must be base64url only (A-Z a-z 0-9 - _ =)"
            )
        return v

    # C-3: ACME email — proper format, no newlines
    @field_validator("acme_email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        if v and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v.strip()):
            raise ValueError("Invalid email format")
        return v.strip() if v else v

    # H-5: ntfy URL — must be http:// or https://, no newlines
    @field_validator("ntfy_url")
    @classmethod
    def valid_url(cls, v: str) -> str:
        if v and not re.match(r"^https?://[^\s\n\r]+$", v):
            raise ValueError("Must be a valid http/https URL")
        return v

    # H-5b: ntfy_topic — alphanumeric, hyphens, underscores only [BR: input not sanitized before structured-file write]
    @field_validator("ntfy_topic")
    @classmethod
    def ntfy_topic_safe(cls, v: str) -> str:
        """Topic must be URL-safe: alphanumeric, hyphens, underscores only."""
        import re as _re

        if not _re.match(r"^[A-Za-z0-9_-]{1,64}$", v):
            raise ValueError("ntfy_topic must be alphanumeric with hyphens/underscores, 1-64 chars")
        return v

    # H-6: cert_resolver — restrict to known enum values
    @field_validator("cert_resolver")
    @classmethod
    def validate_cert_resolver(cls, v: str) -> str:
        _valid = {"letsencrypt", "zerossl", "buypass", "staging"}
        if v and v not in _valid:
            raise ValueError(
                "Unknown cert_resolver '" + v + "'. Allowed values: " + ", ".join(sorted(_valid))
            )
        return v

    # M-7: network_name — allowlist (letters, digits, hyphen, underscore only)
    @field_validator("network_name")
    @classmethod
    def network_name_safe(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("network_name must not be empty")
        if not _NETWORK_NAME_RE.match(v):
            raise ValueError(
                "network_name may only contain letters, digits, hyphens, and underscores"
            )
        return v
