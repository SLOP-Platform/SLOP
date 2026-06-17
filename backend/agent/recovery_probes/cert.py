"""backend/agent/recovery_probes/cert.py — TLS certificate expiry GROUND probe.

Probe 3: cert_expiry
    If the app manifest exposes a ``tls_cert_path`` field, the cert must not
    expire within 30 days.  DRIFT < 30 days (warn), DRIFT < 7 days (crit).
    INDETERMINATE if the cert file is unreadable.
    Supports both PEM files and Traefik ACME JSON (auto-detected by extension).
    The ``{config_root}`` template in cert paths is resolved at reconcile time.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from backend.agent.spine import Finding, Verdict

_CERT_WARN_DAYS = 30  # DRIFT (warn)
_CERT_CRIT_DAYS = 7  # DRIFT (crit)


def _cert_not_after_pem(cert_path: str) -> datetime.datetime | None:
    """Parse PEM cert expiry via ssl stdlib or openssl subprocess.

    Returns the expiry datetime (UTC-naive) or None if unreadable.
    """
    # Try ssl stdlib first (no subprocess, preferred)
    try:
        import ssl

        cert = ssl._ssl._test_decode_cert(cert_path)  # type: ignore[attr-defined]
        not_after_str = cert.get("notAfter", "")
        if not_after_str:
            # Format: "Jan  1 00:00:00 2030 GMT"
            return datetime.datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z")
    except Exception:  # noqa: S110  # nosec B110  best-effort ssl parse; fall through to openssl subprocess
        pass

    # Fallback: openssl subprocess
    try:
        import subprocess

        r = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate", "-in", cert_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            # Output: "notAfter=Jan  1 00:00:00 2030 GMT"
            line = r.stdout.strip()
            if "=" in line:
                date_str = line.split("=", 1)[1].strip()
                return datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
    except Exception:  # noqa: S110  # nosec B110  best-effort subprocess fallback; return None if unavailable
        pass

    return None


def _der_not_after(der_bytes: bytes) -> datetime.datetime | None:
    """Parse notAfter from a DER-encoded certificate via openssl subprocess.

    Returns the expiry datetime (UTC-naive) or None if unreadable.
    """
    try:
        import subprocess

        r = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate", "-inform", "DER"],
            input=der_bytes,
            capture_output=True,
            timeout=5,
        )
        if r.returncode != 0:
            return None
        line = r.stdout.decode().strip()
        if "=" not in line:
            return None
        date_str = line.split("=", 1)[1].strip()
        return datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
    except Exception:  # nosec B110  # best-effort subprocess; caller handles None
        return None


def _decode_b64_cert(cert_b64: str) -> bytes | None:
    """Decode a base64 cert string to DER bytes, or None if malformed."""
    import base64

    try:
        return base64.b64decode(cert_b64)
    except Exception:  # nosec B110  # best-effort decode; None signals failure to caller
        return None


def _acme_cert_entries(data: dict[str, Any]) -> list[bytes]:
    """Extract all DER-decoded certificate bytes from a Traefik ACME JSON dict."""
    result: list[bytes] = []
    for resolver_data in data.values():
        if not isinstance(resolver_data, dict):
            continue
        for cert_entry in resolver_data.get("Certificates") or []:
            if not isinstance(cert_entry, dict):
                continue
            cert_b64 = cert_entry.get("certificate") or cert_entry.get("Certificate") or ""
            if not cert_b64:
                continue
            der = _decode_b64_cert(cert_b64)
            if der is not None:
                result.append(der)
    return result


def _cert_not_after_acme_json(acme_path: str) -> datetime.datetime | None:
    """Parse the earliest cert expiry from a Traefik ACME JSON store.

    Traefik stores ACME certs as:
      {<resolver>: {"Certificates": [{certificate: "<base64-DER>", ...}]}}

    Returns the earliest (soonest to expire) notAfter datetime found across all
    certs in all resolvers, or None if the file is unreadable or contains no certs.
    """
    import json

    try:
        data = json.loads(Path(acme_path).read_text(encoding="utf-8"))
    except Exception:  # nosec B110  # best-effort; caller handles None
        return None

    if not isinstance(data, dict):
        return None

    earliest: datetime.datetime | None = None
    for der_bytes in _acme_cert_entries(data):
        dt = _der_not_after(der_bytes)
        if dt is not None and (earliest is None or dt < earliest):
            earliest = dt
    return earliest


def _cert_not_after(cert_path: str) -> datetime.datetime | None:
    """Parse cert expiry from a PEM file or a Traefik ACME JSON store.

    Auto-detects format: files ending in ``.json`` are treated as Traefik
    ACME JSON; all others are tried as PEM first (ssl stdlib) then via openssl.

    Returns the expiry datetime (UTC-naive) or None if unreadable.
    """
    if cert_path.lower().endswith(".json"):
        return _cert_not_after_acme_json(cert_path)
    return _cert_not_after_pem(cert_path)


def _probe_cert_expiry(app: Any, config_root: str = "") -> Finding | None:
    """GROUND: TLS certificate not expiring within thresholds.

    Supports PEM files and Traefik ACME JSON stores (auto-detected by extension).
    The ``{config_root}`` template in cert paths is resolved using ``config_root``
    when provided; unresolved templates yield INDETERMINATE.

    Returns None when the manifest has no ``tls_cert_path`` field (omit).
    """
    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.cert_expiry.{app_key}"
    physics = f"TLS certificate expiry for app {app_key}"

    cert_path = getattr(app, "tls_cert_path", None) or ""
    if not cert_path:
        return None  # no cert configured — omit finding

    # Resolve {config_root} template if present
    if "{config_root}" in cert_path:
        if not config_root:
            return Finding(
                id=finding_id,
                physics=physics,
                verdict=Verdict.INDETERMINATE,
                summary="tls_cert_path uses {config_root} template but config_root not provided",
                detail=f"raw_path={cert_path}",
            )
        cert_path = cert_path.replace("{config_root}", config_root)

    if not Path(cert_path).exists():
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="tls_cert_path declared but file absent",
            detail="cert_path not present on disk",
        )

    not_after = _cert_not_after(cert_path)
    if not_after is None:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="cert expiry unreadable",
            detail="ssl and openssl both failed to parse the cert",
        )

    now_utc = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    days_left = (not_after - now_utc).days

    if days_left < _CERT_CRIT_DAYS:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"certificate expires in {days_left} days — critical",
            detail=f"days_remaining={days_left} threshold_crit={_CERT_CRIT_DAYS}",
        )
    if days_left < _CERT_WARN_DAYS:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"certificate expires in {days_left} days — warn",
            detail=f"days_remaining={days_left} threshold_warn={_CERT_WARN_DAYS}",
        )
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"certificate valid for {days_left} more days",
        detail=f"days_remaining={days_left}",
    )
