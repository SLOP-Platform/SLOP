# SLOP Core Processes Adversarial Audit

## Original review prompt

> You are a senior programmer doing an adversarial audit/review of the overall
> structure and function of the SLOP core processes. SLOP is a project that uses
> an AI Agent as the brain that manages all aspects of the project: it manages
> apps, deployments, errors, fixes problems, and if it cannot, researches and
> finds solutions that work. It is self-learning, focused on its functions.
> SLOP focuses on clean, clear code using simple solutions where possible. It is
> not a professional app; it is aimed at home hosting for non-coders. It should
> be modular and upgradable without breaking. Code should be optimized for
> performance on various hardware. This review is read-only; do not make changes.

## Review scope

This audit reviewed the repository structure and core operational paths for:

- FastAPI backend and API control plane
- Agent, health, diagnosis, and remediation loops
- Docker/Compose deployment and runtime supervision
- SQLite state and startup reconciliation behavior
- Vue frontend process visibility and operator UX
- Installer, upgrade, migration, and documentation consistency

The review focused on adversarial risk: places where SLOP's stated goals
could fail for a non-coder home-hosting user.

## Executive summary

SLOP has a promising overall shape: a FastAPI backend, Vue UI, SQLite state,
manifest-driven Docker Compose fragments, supervised background loops, and an
installer state model. The codebase also shows good instincts around state
centralization, structured migrations, supervised tasks, and a carefully
designed agent spine egress boundary.

The core risk is fragmentation. Several important operational paths are not
using the same assumptions:

- The management API is open by default.
- Runtime failure detection assumes Compose-style container names, while SLOP
  emits explicit container names.
- Remediation is split across multiple taxonomies and apply paths.
- Upgrade documentation describes tools and git workflows that do not exist in
  the installed v5 model.
- The frontend can show setup, progress, and health states that are stale or
  misleading.

For a project aimed at non-coders, these issues matter more than missing
features. They affect whether the system can be trusted when something goes
wrong.

## Critical findings

### 1. Control plane is unauthenticated by default

**Evidence**

- `backend/api/main.py:255-256` only logs that auth is missing.
- `backend/api/main.py:329-335` defaults CORS to `["*"]`.

**Why it matters**

SLOP can install applications, restart containers, apply fixes, reset platform
state, and manage settings. If the service is reachable on a LAN or exposed by
mistake, any client that reaches the API can likely operate the platform.

**Impact**

This is the highest-risk structural issue for a home-hosting management system.
The product assumes a trusted network but does not enforce one.

**Recommended direction**

Add a real default-safe access control story. At minimum, production installs
should require authentication or bind to localhost until explicitly configured.

### 2. Docker event watcher likely misses normal app failures

**Evidence**

- Watcher expects names like `slop-<app>-<replica>`:
  `backend/agent/watcher.py:36-47`.
- Compose fragments set explicit container names to the manifest key:
  `backend/core/compose.py:541-544`.
- Non-matching containers are ignored:
  `backend/agent/watcher.py:68-71`.

**Why it matters**

The watcher is supposed to detect runtime events such as `die`, `oom`, and
`health_status=unhealthy`. If normal SLOP app containers do not match the
watcher's naming pattern, the agent pipeline never sees those failures.

**Impact**

This undermines the core promise that SLOP detects and reacts to running app
failures.

**Recommended direction**

Resolve container identity centrally. Runtime detection should use SLOP labels,
database records, or a shared container-name resolver instead of hard-coded
Compose naming assumptions.

### 3. Container identity is inconsistent across remediation paths

**Evidence**

- Agent safe apply restarts by `app_key`:
  `backend/agent/apply.py:234-240`.
- AI safety restarts by `app_key`:
  `backend/core/ai_safety.py:116-120`.
- Manifest self-heal restarts by `app_key`:
  `backend/health/checker.py:389-392`.
- Some API code correctly uses `app.container_name or key`:
  `backend/api/apps.py:2126-2129`.

**Why it matters**

Some manifests and companion services define custom container names. A fix path
that uses `app_key` can fail, restart the wrong target, or report an incorrect
result.

**Impact**

Self-healing becomes unreliable exactly when the user needs it most.

**Recommended direction**

Create one helper for resolving the operational container target from app key,
DB row, manifest, or labels. Use it everywhere: logs, restart, reload, verify,
health, watcher, and apply.

### 4. Remediation systems use incompatible taxonomies

**Evidence**

- Health LLM writes pending fixes using `action_type`:
  `backend/health/checker_llm.py:391-405`.
- Health approval executes `action_type` through AI safety:
  `backend/api/health.py:1919-1925`.
- Agent apply maps `diagnosis_class` to a fix type:
  `backend/agent/api.py:94-126`,
  `backend/agent/apply.py:42-54`.
- Install-failure listener writes `diagnosis_class`:
  `backend/agent/listener.py:124-138`.
- Scheduler auto-apply filters by `diagnosis_class`:
  `backend/agent/autofix.py:47-72`.

**Why it matters**

SLOP currently has multiple ways to represent and apply "a fix." Some are based
on `action_type`; others are based on `diagnosis_class`. Some use the AI safety
gate; others use safe-fix mapping; manifest self-heal has its own path.

**Impact**

For a non-coder, this creates confusing behavior:

- Some fixes can be approved.
- Some auto-apply.
- Some return "requires human approval."
- Some never become eligible for auto-apply.

**Recommended direction**

Unify remediation into one pending-fix schema, one safety model, and one apply
path. Each fix should have a clear source, confidence, risk tier, target,
command/action, verification method, and final status.

### 5. v5 upgrade story is structurally broken

**Evidence**

- v5 installer removes `.git`:
  `installer/fetch.py:166-182`.
- Docs say `/opt/slop` is a git clone updated with `ms-update` or
  `deploy.sh --update`:
  `docs/INSTALL.md:3-8`.
- `deploy.sh` sources a missing helper:
  `deploy.sh:49-54`.
- `ms-update` and `tools/` were not present in this checkout.

**Why it matters**

SLOP is supposed to be modular and upgradable without breaking. The installed
v5 model is a tag-pinned snapshot without `.git`, while the docs describe a
git-based update workflow using missing tools.

**Impact**

Users and agents cannot reliably update the platform. The documented path and
implemented install model disagree.

**Recommended direction**

Declare one supported v5 upgrade path and remove or clearly mark obsolete
deployment models. If the intended model is reinstall-by-tag with preserved
data, document that explicitly and test it.

## High-priority findings

### 6. Startup cleanup can silently delete legitimate state

**Evidence**

- Startup cleanup deletes app rows, health history, operations, and pending
  fixes when the compose fragment is missing:
  `backend/api/main.py:152-199`.

**Why it matters**

If the data directory, compose directory, mount, restore, or filesystem is
temporarily wrong, SLOP can delete useful state instead of preserving it for
operator review.

**Recommended direction**

Mark records as `orphaned` or `needs_reconcile` instead of deleting them on
startup. Require explicit operator action to remove history.

### 7. Manifest self-heal bypasses the shared safety model

**Evidence**

- Code explicitly states manifest `self_heal` bypasses AI safety tiers:
  `backend/health/checker.py:520-530`.

**Why it matters**

Manifest-defined automatic remediation may be reasonable for expert users, but
SLOP targets non-coders. Any autonomous mutation path should be visible in the
same safety posture as other agent actions.

**Recommended direction**

Route manifest self-heal through the same safety and audit pipeline, or clearly
model it as an owner-approved policy with explicit UI visibility.

### 8. LLM egress safety is uneven

**Evidence**

- Spine egress has a strong allowlisted boundary:
  `backend/agent/spine_egress.py:1-11`.
- Classifier/health paths route prompts and logs through dispatcher with scrub
  preserved:
  `backend/agent/classifier.py:167-180`.

**Why it matters**

Logs can contain paths, hostnames, tokens, local topology, usernames, and
service details. The spine has a strong structural allowlist, but other LLM
paths appear to rely on prompt/log scrubbing.

**Recommended direction**

Put health and classifier LLM calls behind the same egress discipline as the
spine, or make cloud egress opt-in with clear disclosure.

### 9. Health scheduler can overload low-end hardware and degrade quietly

**Evidence**

- Scheduler skips cycles if the previous one is still running:
  `backend/health/scheduler.py:524-527`.
- Each cycle runs a large set of post-cycle probes concurrently:
  `backend/health/scheduler.py:543-559`.

**Why it matters**

SLOP targets varied home hardware, including low-resource systems. Expensive
health checks, source scans, CVE probes, image checks, and remediation attempts
can compete with the actual applications.

**Recommended direction**

Split probes into budgets and cadences. Surface skipped cycles and stale
health prominently in the UI.

### 10. Frontend can show stale or misleading process state

**Evidence**

- Platform errors are stored but the shell shows setup-required when not ready:
  `frontend/src/stores/platform.ts:14-21`,
  `frontend/src/App.vue:46-55`.
- Setup wizard polling has no overall timeout:
  `frontend/src/views/SetupView.vue:2081-2098`.
- Catalog progress is time-based:
  `frontend/src/views/CatalogView.vue:1079-1099`.
- Dashboard marks Traefik running from a stored version field:
  `frontend/src/views/DashboardView.vue:352-353`.

**Why it matters**

Non-coders need truthful state, not optimistic state. Backend offline, setup
required, stale health, install running, install failed, and fix pending should
all look different.

**Recommended direction**

Make all long-running process state terminal and explicit. Prefer step-based
progress over time-based progress. Add global banners for backend unreachable
and stale scheduler data.

## Medium-priority findings

### 11. Documentation is not reliable enough for agent-managed operations

**Evidence**

- `docs/MAP.md:55-65` references missing docs such as `DEPLOY.md`, ADRs,
  backlog, release notes, and lessons.
- `migrations/README.md:14-20` lists only migrations 001-003 while the repo
  contains migrations through 017.
- `README.md:79-80` links release notes that were not present.

**Why it matters**

A self-learning project needs accurate operational memory. Stale docs become
bad input for future agents and misleading runbooks for users.

**Recommended direction**

Treat operational docs as part of the product. Remove missing references or
restore the referenced files. Keep migration and upgrade docs generated or
checked in CI.

### 12. Cloud agent health check is too shallow

**Evidence**

- Cloud provider status is treated as running if an API key exists:
  `backend/core/agent.py:204-212`.

**Why it matters**

An API key does not prove provider reachability, quota, model availability, or
successful inference.

**Recommended direction**

Use a lightweight live probe for configured cloud providers and distinguish
configured, reachable, degraded, and failed states.

### 13. SQLite durability tradeoff should be visible in recovery UX

**Evidence**

- SQLite uses WAL and `synchronous=NORMAL`:
  `backend/core/state_db.py:35-38`.

**Why it matters**

This is a reasonable homelab performance choice, but power loss or crash can
still leave ambiguous operation state. Users need clear recovery language.

**Recommended direction**

Keep the performance choice, but make recovery states explicit after restart:
interrupted install, unknown app state, retry available, and manual review
required.

## Positive structural observations

- Centralized state access through `StateDB` is a good foundation.
- Structured migrations with checksum immutability are directionally strong.
- Supervised background tasks show the right operational instinct.
- The agent spine egress design is much stronger than a simple regex-scrub
  model.
- Manifest-driven Compose fragments are a good modularity boundary for apps.
- Startup recovery for orphaned install progress shows awareness of real-world
  interruption cases.

## Recommended priority order

1. Add real auth or default-safe binding before treating SLOP as usable beyond
   localhost.
2. Fix container identity centrally and use it everywhere.
3. Unify remediation into one pending-fix schema, one safety model, and one
   apply path.
4. Replace destructive startup cleanup with explicit orphan/reconcile state.
5. Ship a real v5 upgrade path or remove/update `ms-update` and git-clone docs.
6. Make frontend state honest and terminal for setup, install, health, and
   fixes.
7. Put all cloud LLM calls behind the same egress discipline as the spine.
8. Add integration tests around watcher -> listener -> pending fix -> apply ->
   verify.

## Bottom line

SLOP's architecture intent is strong, but the most important operational paths
are not yet coherent enough for the stated audience. The system needs fewer
parallel control paths, stronger defaults, and more truthful process state.

The fastest way to improve trust is not to add more agent behavior. It is to
make the existing behavior consistent, auditable, and safe by default.
