# SLOP Observability

This document covers the operator-side observability surface of SLOP v4. It complements the in-code documentation in `backend/core/logging.py` (structured logs) and `backend/core/metrics.py` (Prometheus metrics).

## Table of contents

1. [Surfaces](#surfaces)
2. [Metrics — `/metrics`](#metrics----metrics)
3. [Health probes — `/healthz` `/readyz` `/startupz`](#health-probes)
4. [Audit log — `/api/v1/audit`](#audit-log)
5. [Structured logs — `correlation_id`](#structured-logs)
6. [Scrape configurations](#scrape-configurations)

---

## Surfaces

| Surface | Path | Purpose | Auth |
|---------|------|---------|------|
| Metrics | `/metrics` | Prometheus exposition format | open (homelab); restrict at reverse proxy if exposed |
| Liveness | `/healthz` | "Is the process alive?" — never blocks on dependencies | open |
| Readiness | `/readyz` | "Should we route traffic to this pod?" — checks DB + critical deps | open |
| Startup | `/startupz` | "Is the process past its initialization phase?" — migrations + scheduler running | open |
| Audit log query | `/api/v1/audit` | Read the immutable mutation trail | admin (future — currently open in single-user mode) |

The four observability paths sit OUTSIDE the `/api/v1/` versioning umbrella because they're operational infrastructure, not the application API. They will not change shape between major SLOP versions without an explicit deprecation cycle.

---

## Metrics — `/metrics`

**Format:** Prometheus exposition format ([spec](https://prometheus.io/docs/instrumenting/exposition_formats/)).
**Source:** auto-instrumented via [`prometheus-fastapi-instrumentator`](https://github.com/trallnag/prometheus-fastapi-instrumentator) plus custom metrics defined in `backend/core/metrics.py`.

### Auto-instrumented (every FastAPI route)

| Metric | Type | Labels | Notes |
|--------|------|--------|-------|
| `http_requests_total` | counter | `method`, `handler`, `status` | Cumulative request count. `handler` is the route template (`/api/v1/apps/{key}`), not the literal URL — bounded cardinality. |
| `http_request_duration_seconds` | histogram | `method`, `handler` | Request duration. Default buckets (`prometheus_client`'s `DEFAULT_BUCKETS`). |
| `http_request_size_bytes` | summary | `method`, `handler` | Request body size. |
| `http_response_size_bytes` | summary | `method`, `handler` | Response body size. |

### Custom (SLOP-specific)

Defined in `backend/core/metrics.py`:

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `slop_install_duration_seconds` | histogram | `app_key`, `outcome` | `backend.manifests.executor.install_app()` end-to-end timing. `outcome` = `success` / `failed`. Buckets cover 1s–10min range. |
| `slop_health_check_duration_seconds` | histogram | `app_key`, `outcome` | `backend.health.checker.check_app()` per-invocation duration. |
| `slop_db_query_duration_seconds` | histogram | `verb` | StateDB query duration. `verb` = SQL verb (SELECT/INSERT/UPDATE/DELETE/DDL/OTHER). Bucketed for sub-millisecond to multi-second range. |
| `slop_errors_total` | counter | `endpoint`, `error_class` | Errors by endpoint + exception type name. Both labels are bounded; raw URLs / dynamic IDs are NOT used as label values. |

### Cardinality discipline

Every label dimension is bounded:

- `handler` is a route template, never a literal URL with IDs in it.
- `app_key` is from the catalog (~50 entries, growing slowly).
- `error_class` is a Python class name (bounded by exception hierarchy size).
- `verb` is a SQL verb (`{SELECT, INSERT, UPDATE, DELETE, DDL, OTHER}` = 6).

Adding a new label that could explode (UUIDs, timestamps, free-form strings) without thinking about cardinality is a Core Rule 8.2 violation.

### Disabling /metrics

The endpoint is enabled by default. To disable in tests or other environments, the instrumentator does NOT currently respect an env var (the `should_respect_env_var` flag is off — SLOP's metrics are cheap enough to always run). If a future deployment needs gating, flip that flag in `backend/api/main.py`.

---

## Health probes — `/healthz` `/readyz` `/startupz`

Kubernetes-style three-tier probes, each answering a different operational question:

- **`/healthz` (liveness):** Is the Python process alive and accepting connections? Always returns `200 OK` with body `{"status": "ok"}`. Never queries dependencies. K8s restarts the pod if this 5xxs or times out.
- **`/readyz` (readiness):** Should the load balancer route traffic to this pod? Checks DB connectivity + state.configure() set + critical dependencies. Returns `200` when ready, `503` with `{"status": "not_ready", "checks": {...}}` otherwise.
- **`/startupz` (startup):** Has the process finished its one-time initialization? Returns `200` once migrations applied + scheduler started; `503` during the startup window. K8s uses this to delay `livenessProbe` until startup completes.

These three are NOT the same as `/api/health/summary` — the `/api/health/...` namespace is the application-level health view (per-app docker container health, LLM agent state, sources scan status). The `/healthz` family is process-level.

(Implementation: step 4.2.b — see `backend/api/probes.py`.)

---

## Audit log — `/api/v1/audit`

Every mutating HTTP request (POST/PUT/DELETE) writes one row to the `audit_log` table:

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | autoincrement |
| `ts` | INTEGER | unix epoch seconds |
| `actor` | TEXT | user identity (single-user homelab → `"local"` for now) |
| `action` | TEXT | HTTP method + route template — e.g. `POST /api/v1/apps/{key}/install` |
| `resource_id` | TEXT | path parameter that identifies the resource (e.g. the `{key}`) |
| `request_body_hash` | TEXT | sha256 of the request body for non-repudiation; full body NOT stored |
| `response_status` | INTEGER | HTTP status code returned |
| `correlation_id` | TEXT | matches `X-Request-ID` for log correlation |

**Reading audits:** `GET /api/v1/audit?since=<unix_ts>&limit=100` returns rows reverse-chronologically. Filtering by `actor`, `action`, `resource_id` is supported via query params.

**Retention:** unlimited by default. The table grows linearly with mutation volume; a homelab sees a few hundred entries per day, so this is fine for years. A future `slop_audit_log_rows_total` metric (custom, low-cardinality) would track this for capacity planning.

(Implementation: step 4.3 — see `backend/api/audit.py`, migration `004_audit_log.sql`.)

---

## Structured logs — `correlation_id`

Every log line carries `correlation_id` (from the `X-Request-ID` request header, or a fresh UUID per request). Set by `CorrelationIdMiddleware`. Cf. ADR 0003 (structured logging).

Cross-correlate metrics + logs by piping logs to a query backend (Loki, Elasticsearch, etc.) and grouping by `correlation_id`. Each Prometheus scrape sample is then traceable to the requests that produced it.

---

## Scrape configurations

### Prometheus

Minimal `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: slop
    metrics_path: /metrics
    static_configs:
      - targets: ['slop.local:8080']
    scrape_interval: 30s
    scrape_timeout: 10s
```

### Grafana dashboard skeleton

Three panel groups make sense for the SLOP signal set:

1. **Request volume + latency** (auto-instrumented metrics) — `rate(http_requests_total[5m])` and `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, handler))`.
2. **SLOP-domain timing** — install duration p95, health check duration p95.
3. **Error rates** — `rate(http_requests_total{status=~"5.."}[5m])` and `rate(slop_errors_total[5m])`.

A starter `dashboard.json` is not included in the repo — Grafana dashboards drift fast and rarely match the intent over time. The metric names + labels above are stable; build dashboards against those.

### Kubernetes probes

```yaml
livenessProbe:
  httpGet:
    path: /healthz
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /readyz
    port: 8080
  periodSeconds: 5
startupProbe:
  httpGet:
    path: /startupz
    port: 8080
  failureThreshold: 30
  periodSeconds: 5
```

The `startupProbe.failureThreshold * periodSeconds = 150s` window covers cold-start migration runs. The `livenessProbe.initialDelaySeconds=5` is intentionally short — by then `/healthz` should always return 200 because it doesn't touch dependencies.

---

## Strategy notes

- Metrics surface lives at `/metrics` (Prometheus convention), not `/api/v1/metrics` — it's not part of the application API and shouldn't carry version semantics.
- Audit log query lives at `/api/v1/audit` — it IS part of the application API (admin tool) and follows the API versioning policy from ADR 0005.
- The boundary: `/api/...` and `/api/v1/...` are application; `/healthz`, `/readyz`, `/startupz`, `/metrics` are infrastructure.
