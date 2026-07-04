"""Per-app managed-DB provisioning (#1210 defect-2).

Single source of the target-database derivation **and** the idempotent per-app
``CREATE DATABASE`` that :func:`backend.manifests.executor._install_dependencies` issues right
after a managed DB engine (postgres/mariadb) is up and before the dependent app container starts.

Why this module exists — the guard-vs-impl drift trap.
    The #1210 *detection* guard (``tests/test_catalog_dependency_coverage.py``) derives each app's
    target database from its catalog env to prove the app points at a database the installer
    actually provisions. The *fix* must create that same database. If the two derivations drift,
    the guard goes green while installs stay broken. So the derivation
    (:func:`app_target_db` / :func:`strip_compose_default` / :data:`MANAGED_DB_ENGINES`) lives HERE
    and is imported by BOTH the executor (to create) and the test (to assert) — one source.

Scope of this slice (option-A design proposal, .claude/run/l3-37-1210-option-a-design-proposal.md):
    - Postgres + mariadb (the ``{postgres, mariadb}`` managed-DB plural set — blast-radius
      parameterized, not postgres-hardcoded).
    - immich is intentionally OUT of scope: it needs a pgvector-capable image the stock ``postgres``
      image lacks, so a plain ``CREATE DATABASE`` does not fix it (tracked separately).

SERVER-VERIFY GATED:
    The docker-exec wiring (psql/mariadb availability inside the managed container, the auth method
    the managed image accepts over the local socket) is confirmed on a real install per the design
    proposal. The pure logic below — target derivation, the create/skip decision, idempotency, and
    identifier validation — is host-independent and unit-tested (``tests/test_db_provision.py``).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from typing import TypedDict
from dataclasses import dataclass


def strip_compose_default(v: str) -> str:
    """'${POSTGRES_USER:-slop}' -> 'slop'; '${X}' -> ''; plain 'slop' -> 'slop'."""
    m = re.fullmatch(r"\$\{[^:}]+:-([^}]*)\}", v)
    if m:
        return m.group(1)
    return "" if v.startswith("${") else v


#: The managed relational-DB engines ``{postgres, mariadb}`` — a KNOWN plural set (the
#: ``_ensure_managed_service`` ``images`` keys minus the no-DB-name redis). Parameterizing the
#: target-DB derivation over this set (rather than hardcoding postgres) is the
#: Reuse-and-blast-radius checkpoint: mariadb has the IDENTICAL latent class (a single shared
#: provisioned DB, no per-app CREATE DATABASE) — it just isn't tripped today (one mariadb app,
#: booklore, whose target matches the provisioned default db ``booklore``). Each engine declares:
#: the service_type (its ``_ensure_managed_service`` branch + container host name), the ordered env
#: keys whose value gives the provisioned default DB name (first present wins; for postgres
#: ``POSTGRES_DB`` is absent so it falls back to ``POSTGRES_USER``, which postgres uses as the
#: implicit default db), the URL schemes its connection strings use, and the dependency flag.
class _EngineSpec(TypedDict):
    service_type: str
    default_db_keys: tuple[str, ...]
    url_schemes: tuple[str, ...]
    dep_key: str


MANAGED_DB_ENGINES: dict[str, _EngineSpec] = {
    "postgres": {
        "service_type": "postgres",
        "default_db_keys": ("POSTGRES_DB", "POSTGRES_USER"),
        "url_schemes": ("postgresql", "postgres"),
        "dep_key": "postgres",
    },
    "mariadb": {
        "service_type": "mariadb",
        "default_db_keys": ("MARIADB_DATABASE",),
        "url_schemes": ("mariadb", "mysql"),
        "dep_key": "mariadb",
    },
}


def app_target_db(env: dict[str, str], engine: str) -> str | None:
    """Extract the database name an app connects to for a managed DB engine, or None if it declares
    no determinable target. Handles the shapes used in the catalog: a URI-style DATABASE_URL
    (``<scheme>://user:pw@host:port/<db>`` — also matches a ``jdbc:<scheme>://...`` URL since the
    ``<scheme>://`` substring is found anywhere), a key=value connection string (``...;Database=<db>;``),
    and an explicit ``*_DB``/``*_DBNAME`` key. The db segment is captured up to a path/query separator
    and ``${VAR:-default}``-stripped, so a hyphenated db is not truncated to a wrong-but-provisioned
    prefix (the false-negative this derivation must avoid) and a compose-defaulted db (booklore's
    ``${MARIADB_DATABASE:-booklore}``) resolves to its real name."""
    schemes = "|".join(MANAGED_DB_ENGINES[engine]["url_schemes"])
    url_re = re.compile(rf"(?:{schemes})://[^/\s]+/([^/\s?#]+)")
    for _key, val in env.items():
        if not isinstance(val, str):
            continue
        m = url_re.search(val)
        if m:
            return strip_compose_default(m.group(1))
        # ADO/Npgsql-style connection string: Database=<db>
        m = re.search(r"(?i)\bdatabase\s*=\s*([A-Za-z0-9_.${:}-]+)", val)
        if m and "://" not in val:
            return strip_compose_default(m.group(1))
    # Explicit DB-name env keys (POSTGRES_DB / MARIADB_DATABASE / <APP>_DB / <APP>_DBNAME)
    for key, val in env.items():
        if not isinstance(val, str) or not val:
            continue
        ku = key.upper()
        if ku in ("POSTGRES_DB", "PGDATABASE", "MARIADB_DATABASE") or ku.endswith(
            ("_DB", "_DBNAME", "_DATABASE_NAME")
        ):
            return strip_compose_default(val)
    return None


#: A managed-DB name we are willing to create. Catalog DB names are simple identifiers
#: (affine/umami/zilean/booklore); restricting to ``[A-Za-z0-9_-]`` means the validated name can be
#: interpolated into a quoted SQL identifier with no injection surface (no quote/semicolon/space/$).
_SAFE_DB_NAME = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of an ``ensure_app_database`` call. ``status`` mirrors the executor step vocabulary:
    ``ok`` (created or already-present or nothing-to-do) / ``warning`` (create attempt failed —
    surfaced, non-fatal, the app may still come up if the DB appears) / ``skipped``."""

    status: str
    message: str
    detail: str = ""
    created: bool = False


# The db name reaches these builders already validated to ``[A-Za-z0-9_-]`` (_SAFE_DB_NAME); it is
# nonetheless passed via psql's ``-v`` client variables (``:'d'`` quotes a literal, ``:"d"`` quotes
# an identifier) so the SQL strings stay STATIC — no Python-side string interpolation into SQL.


def _pg_exists_cmd(container: str, db: str) -> list[str]:
    return [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        "slop",
        "-tAc",
        "-v",
        f"d={db}",
        "SELECT 1 FROM pg_database WHERE datname = :'d'",
    ]


def _create_cmd(engine: str, container: str, db: str) -> list[str]:
    if engine == "postgres":
        # postgres has no CREATE DATABASE IF NOT EXISTS — caller pre-checks existence.
        # :"d" → psql double-quotes the identifier (db value supplied via -v, validated).
        return [
            "docker",
            "exec",
            container,
            "psql",
            "-U",
            "slop",
            "-v",
            f"d={db}",
            "-c",
            'CREATE DATABASE :"d"',
        ]
    # mariadb supports IF NOT EXISTS — inherently idempotent. mariadb's CLI has no psql-style client
    # variable; the identifier is validated to [A-Za-z0-9_-] (_SAFE_DB_NAME) and backtick-quoted.
    return ["docker", "exec", container, "mariadb", "-e", f"CREATE DATABASE IF NOT EXISTS `{db}`"]


def _pg_db_exists(container: str, db: str, run: Callable[..., object]) -> bool:
    p = run(_pg_exists_cmd(container, db), capture_output=True, text=True, timeout=30)
    return getattr(p, "returncode", 1) == 0 and (getattr(p, "stdout", "") or "").strip() == "1"


def ensure_app_database(
    engine: str,
    app_env: dict[str, str],
    *,
    provisioned_default: str,
    container: str | None = None,
    run: Callable[..., object] = subprocess.run,
) -> ProvisionResult:
    """Idempotently ensure the per-app database named in ``app_env`` exists in the managed ``engine``
    container. No-op (``skipped``) when the app names no target or targets the provisioned default DB
    (which the engine already created). Never drops or alters an existing database — safe to re-run
    on an existing install (the create is gated on a not-exists check / ``IF NOT EXISTS``).

    ``provisioned_default`` — the DB the managed engine already provisions (postgres: ``slop`` from
    POSTGRES_USER; mariadb: the MARIADB_DATABASE default). ``run`` is injected for testing.
    """
    if engine not in MANAGED_DB_ENGINES:
        raise ValueError(f"unknown managed DB engine: {engine!r}")
    target = app_target_db(app_env, engine)
    if not target or target == provisioned_default:
        return ProvisionResult("skipped", f"No per-app {engine} database to create.")
    if not _SAFE_DB_NAME.fullmatch(target):
        # Refuse to interpolate a non-identifier into SQL — surface, do not create.
        return ProvisionResult(
            "warning",
            f"Refusing to provision {engine} database with unsafe name.",
            detail=f"target={target!r} is not a [A-Za-z0-9_-] identifier (#1210 safety).",
        )
    ctr = container or MANAGED_DB_ENGINES[engine]["service_type"]
    try:
        if engine == "postgres" and _pg_db_exists(ctr, target, run):
            return ProvisionResult("ok", f"Postgres database {target!r} already present.")
        p = run(_create_cmd(engine, ctr, target), capture_output=True, text=True, timeout=60)
        if getattr(p, "returncode", 1) != 0:
            return ProvisionResult(
                "warning",
                f"Could not create {engine} database {target!r}.",
                detail=((getattr(p, "stderr", "") or getattr(p, "stdout", "") or "")[:300]),
            )
        return ProvisionResult("ok", f"Provisioned {engine} database {target!r}.", created=True)
    except Exception as e:  # subprocess timeout / docker missing — surface, non-fatal
        return ProvisionResult(
            "warning", f"Could not create {engine} database {target!r}.", detail=str(e)[:300]
        )
