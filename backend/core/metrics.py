"""backend/core/metrics.py — Prometheus metrics (step 4.1).

Defines the SLOP-specific custom metrics that supplement the
auto-generated request-level metrics produced by the in-tree HTTP
metrics middleware (`backend/api/http_metrics.py`). That middleware
covers the generic dimensions (HTTP method, route template, status
code, duration) — this module adds the domain-specific signal:

  - install_duration_seconds:    end-to-end install pipeline duration
  - health_check_duration_seconds: per-app health check duration
  - db_query_duration_seconds:   StateDB query duration (broad bucket)
  - errors_total:                error counts by `endpoint`+`error_class`

Each metric is registered against the default `prometheus_client`
registry, which is what the FastAPI instrumentator scrapes. They're
defined at import time (cheap; no I/O) so the symbols exist as soon
as `from backend.core.metrics import ...` runs.

Convention: every label cardinality is bounded — `endpoint` resolves
to a route template (`/api/v1/apps/{key}`) not a literal URL. Avoid
unbounded label spaces (raw URLs with IDs explode cardinality).

See `docs/observability.md` for the operator-side scrape config.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram


# ── Install pipeline duration ─────────────────────────────────────────────
# install_app() runs through 7 phases (validate → deps → config_dir →
# fragment → deploy → post_deploy → register). The full pipeline
# includes docker-compose-up, which dominates the latency. Buckets
# tuned to the typical 5s-5m range for non-trivial images.
install_duration_seconds = Histogram(
    "slop_install_duration_seconds",
    "End-to-end app-install pipeline duration",
    labelnames=("app_key", "outcome"),
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)


# ── Health-check duration ────────────────────────────────────────────────
# Per check_app() invocation. Most checks complete in <1s; the LLM
# diagnose path can run 10-30s when triggered.
health_check_duration_seconds = Histogram(
    "slop_health_check_duration_seconds",
    "Single check_app() invocation duration",
    labelnames=("app_key", "outcome"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


# ── DB query duration ────────────────────────────────────────────────────
# Coarse — every StateDB.execute() / fetchone() / fetchall() is a
# query. The label captures the SQL verb so query mix can be inspected.
db_query_duration_seconds = Histogram(
    "slop_db_query_duration_seconds",
    "StateDB query duration",
    labelnames=("verb",),  # SELECT / INSERT / UPDATE / DELETE / DDL / OTHER
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)


# ── Errors by endpoint + class ───────────────────────────────────────────
# Anything that bubbles up to a 5xx response or that backend code
# catches with `log.error`. `endpoint` uses the FastAPI route
# template (bounded cardinality); `error_class` uses the exception
# type name (also bounded).
errors_total = Counter(
    "slop_errors_total",
    "Errors by endpoint and exception class",
    labelnames=("endpoint", "error_class"),
)


__all__ = [
    "db_query_duration_seconds",
    "errors_total",
    "health_check_duration_seconds",
    "install_duration_seconds",
]
