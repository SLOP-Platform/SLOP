# SLOP Glossary

**Status:** Active — terminology of record for the SLOP project.
**Authority:** When a term defined here appears in another project document with a different meaning, this glossary wins. Update the other doc to the preferred form, or extend the entry here if the new use is legitimate — but do not let the discrepancy persist.

## Purpose

This glossary exists to prevent terminology drift across long-running planning, architecture, and process documents. The motivating incident: "Docker migration" was used in two distinct senses in v4.x docs and was not caught until the v4.1.0 audit-gate review. The same pattern recurs for `tier` (three in-use senses), `audit` (multiple), `step` (two), and others — each a case where a future contributor or future operator could mis-read a doc and act on the wrong meaning. The glossary is the canonical disambiguation surface and the authoritative source for terms no other doc owns.

## How to use this glossary

- **Mid-work lookup.** Scrolling for "what does this term mean here?" — the entry's **Definition** and **Scope** answer in five seconds.
- **Release-tag audit.** Vocabulary sweep before tagging a release — search major docs (CORE_RULES.md, the active cleanup or hardening plan, README) for any bare use of a term in the [Disambiguation index](#disambiguation-index), confirm each use matches a glossary sense, replace with the **Preferred form** where ambiguous. Formalized as Step 3.2 of `HARDENING_V4_2_PLAN.md`.
- **Onboarding.** Read front-to-back, then follow **Authoritative source** pointers into ADRs, Core Rules, and strategy docs for the underlying machinery.

## Adding or modifying entries

A term belongs in this glossary when at least one of:

1. It appears in two or more project documents.
2. It has been observed to drift, or has demonstrated potential to drift (multiple in-use meanings or ambiguous bare use).
3. It is a project-specific term whose meaning is not recoverable from surrounding context alone.

When adding:

- Every sense of an ambiguous term gets its own H3 entry, qualified by parenthetical scope (e.g., `### tier (cleanup or hardening phase)`). All sense entries cross-reference each other via **Do not confuse with**.
- Every entry names an **Authoritative source**. When no other doc owns the definition, the glossary itself is the authoritative source — and a release-tag audit then checks other docs against this entry.
- For ambiguous terms, every sense entry names a **Preferred form** — the unambiguous label recommended in new writing.
- Cross-link related entries via **See also**.

When retiring a term, prefer marking it RETIRED with redirects to the preferred forms rather than deleting. Older commit messages and historical docs still grep for the bare term and need to land somewhere.

When a release-tag audit (Step 3.2 of `HARDENING_V4_2_PLAN.md`) surfaces a new drift case not covered here, extend this glossary in the same commit as the audit doc. Adding entries is the structural response to terminology drift, mirroring the rule-registry response to structural drift (Rule 5.24) and the rule-addition contract for Core Rules (Rule 5.25).

## Disambiguation index

Bare terms with multiple in-use senses. Use the qualified form in new writing:

- **audit** → [audit (general)](#audit-general), [audit gate](#audit-gate), [ms-audit](#ms-audit)
- **Docker migration** → [RETIRED](#docker-migration-retired); use *v4.0.0 containerization* or *future containerization*
- **rule** → [Core Rule](#core-rule), [rule entry (ms-coverage)](#rule-entry-ms-coverage), [structural anti-pattern rule](#structural-anti-pattern-rule), [Semgrep rule](#semgrep-rule)
- **smoke test** → [smoke test (installer)](#smoke-test-installer) (runtime readiness check at install time); general QA usage (shallow pre-test triage) is NOT this project's meaning
- **step** → [step (cleanup sub-division)](#step-cleanup-sub-division), [step (process function)](#step-process-function)
- **tier** → [tier (cleanup or hardening phase)](#tier-cleanup-or-hardening-phase), [tier (rate-limit category)](#tier-rate-limit-category)

## Entries

### [OPUS] / [SONNET] tag

**Definition.** Inline marker on a sub-task in a cleanup or hardening plan specifying which Claude model is expected to execute the work. `[OPUS]` covers design, diagnosis, judgment calls, and ambiguity resolution; `[SONNET]` covers mechanical implementation, pattern application, and file edits where the pattern is well-documented.

**Scope.** Cleanup and hardening plan sub-task lists (v4.x plans moved to slop-process repo); equivalent v5+ plans.

**Authoritative source.** The "Model assignments" sections of the v4.x cleanup and hardening plans (identical wording; plans moved to slop-process private repo).

**See also.** [model gate](#model-gate), [operator](#operator).

---

### ADR (Architecture Decision Record)

**Definition.** A numbered durable record at <!-- TEMPLATE: docs/adr/NNNN-name.md --> (e.g., `docs/adr/0001-database-migrations.md`) documenting the *why* of an architectural decision in Context / Decision / Consequences / Status format. ADRs are immutable once accepted; superseded ADRs stay in the directory with `Status: Superseded` and a `Supersedes:` link in the replacement.

**Scope.** All decisions that constrain code structure: library choices, thresholds, exception clauses, enforcement mechanisms.

**Authoritative source.** Core Rule 4.15; `docs/adr/template.md`.

**Aliases.** Architecture Decision Record (formal); `docs/adr/` (informal directory reference).

**Do not confuse with.** [strategy doc](#strategy-doc) — strategy describes HOW (sequenced, not durable); ADR describes WHY (durable, immutable).

**See also.** [strategy doc](#strategy-doc), [Core Rule](#core-rule).

---

### audit (general)

**Definition.** The act of independently reviewing project state against a known set of rules, terminology, or invariants. Used as both verb ("audit the docs against the glossary") and noun ("the v4.1.0 audit surfaced 9 drift incidents").

**Scope.** Cleanup and hardening plans, Core Rules ("review against core rules"), post-incident analyses.

**Authoritative source.** This glossary.

**Do not confuse with.** [ms-audit](#ms-audit) (the tool), [audit gate](#audit-gate) (a specific release-process checkpoint), Rule 4.21 audit trail / `audit_log` table (the HTTP-request log).

**See also.** [audit gate](#audit-gate), [ms-audit](#ms-audit).

---

### audit gate

**Definition.** Release-tag-time independent review of project state against rules, terminology, and structural invariants. Produces an audit document (e.g., `COMPLETION_AUDIT.md` for v4.1.0) that surfaces drifts and findings before tag creation. Distinct from per-commit enforcement.

**Scope.** Release process at v4.x.x and v5+ tag creation; Rule 5.24 ("release-tag-gate audit"); `HARDENING_V4_2_PLAN.md` Tier 1 Step 1.4.i and Tier 3 Step 3.2.

**Authoritative source.** The release process doc (moved to slop-process private repo; section 3 documents all four pre-release gates; section 4 documents the vocabulary sweep procedure); this glossary for canonical disambiguation.

**Aliases.** [release-tag gate](#release-tag-gate); tag-gate; tag-gate review.

**Do not confuse with.** [pre-flight verification](#pre-flight-verification) (release-tag-time tag-prompt checklist; another distinct checkpoint).

**See also.** [pre-flight verification](#pre-flight-verification), [drift pattern](#drift-pattern).

---

### bookkeeping discipline

**Definition.** The class of structural enforcement targeted by v4.2 hardening: ensuring that documentation, ledgers, and cross-doc invariants stay in sync via the same scan-staged-changes pattern used for code-level rules. Distinct from architectural discipline (Core Rules 1.x–4.x) and process discipline (5.x).

**Scope.** `HARDENING_V4_2_PLAN.md` Premise; informs Rules 5.23, 5.24, 5.25.

**Authoritative source.** `HARDENING_V4_2_PLAN.md` Premise.

**See also.** [hardening (v4.2)](#hardening-v42), [structural enforcement](#structural-enforcement), [rule-addition contract](#rule-addition-contract).

---

### cleanup arc

**Definition.** The v4.1.0 software-engineering improvement effort comprising the four cleanup tiers documented in `PROJECT_CLEANUP.md`. Completed; the v4.2 hardening phase is its bookkeeping-layer successor.

**Scope.** v4.1.0 tag and the work that preceded it; referenced retrospectively in `HARDENING_V4_2_PLAN.md` and `CORE_RULES.md` lessons learned.

**Authoritative source.** The v4.x PROJECT_CLEANUP.md plan (moved to slop-process private repo).

**Do not confuse with.** [cleanup migration](#cleanup-migration) (a Rule 5.19 two-migration sequence; unrelated), the `chore:` Conventional Commits type (one specific commit category).

**See also.** [hardening (v4.2)](#hardening-v42), [tier (cleanup or hardening phase)](#tier-cleanup-or-hardening-phase).

---

### cleanup migration

**Definition.** The second migration in a widen-then-clean two-migration sequence for non-backward-compatible schema changes. The first migration widens the schema; the cleanup migration removes the old shape after the wide phase has propagated to all deployments.

**Scope.** Rule 5.19 (Migration Discipline) discussions of column drops, CHECK-constraint tightenings, and NOT NULL additions on populated columns.

**Authoritative source.** Core Rule 5.19.

**Do not confuse with.** [cleanup arc](#cleanup-arc) (the v4.1.0 effort).

---

### companion change (C1–C5)

**Definition.** One of the five changes that must land in the same commit as a new numeric Core Rule, per the rule-addition contract: (C1) `### N.NN` heading in `CORE_RULES.md` plus matching `ms-coverage` RULES entry, (C2) Section 8 version-history row, (C3) regenerated coverage_map.json node, (C4) updated `tests/__snapshots__/test_cli_snapshots.ambr`, (C5) `test_fn` / `test_file` fields pointing to an existing test function.

**Scope.** Every commit that adds a numeric Core Rule.

**Authoritative source.** Core Rule 5.25; ADR 0012.

**See also.** [rule-addition contract](#rule-addition-contract), [Core Rule](#core-rule), [three-layer pattern](#three-layer-pattern).

---

### compose fragment / fragment

**Definition.** A YAML file generated by an infra provider under `config.data_dir/compose/` and included in the runtime docker-compose deployment. Distinct from a catalog manifest (which is hand-authored input; the fragment is generated output).

**Scope.** Backend infra layer (`backend/infra/providers/*.py`); Core Rule 1.7 (resource conflict prevention); Core Rule 4.12 (a `tmp_path`-rooted compose dir is the canonical real fake for tests).

**Authoritative source.** Core Rule 1.7; ADR 0009 (infra-slot abstraction).

**Aliases.** "fragment" (bare; unambiguous in infra context).

**Do not confuse with.** [manifest](#manifest) (catalog input, hand-authored).

**See also.** [manifest](#manifest), [infra slot](#infra-slot).

---

### Core Rule

**Definition.** A numbered architectural rule in CORE_RULES.md (moved to the slop-process private repo), structured under Sections 1–5 (verifiable rules) or Section 6 (guidance). Each verifiable Core Rule pairs with an `ms-coverage` rule entry naming the proving test function.

**Scope.** All architectural constraints that have caused or could cause a class of bug.

**Authoritative source.** CORE_RULES.md (moved to the slop-process private repo).

**Aliases.** Some Core Rules carry both as-shipped numbers (e.g., 5.22) and planned-section numbers (e.g., 8.1) — see [renumbering pattern](#renumbering-pattern-core-rules).

**Do not confuse with.** [rule entry](#rule-entry-ms-coverage) (the machine-checkable `ms-coverage` companion), [structural anti-pattern rule](#structural-anti-pattern-rule) (the anti-pattern registry; moved to slop-process), [Semgrep rule](#semgrep-rule) (`.semgrep/rules/*.yml`). All four are "rules" in the project; only Core Rules carry the `N.NN` heading and the rule-addition contract.

**See also.** [rule entry (ms-coverage)](#rule-entry-ms-coverage), [rule-addition contract](#rule-addition-contract), [renumbering pattern](#renumbering-pattern-core-rules).

---

### cutoff SHA

**Definition.** Commit `4e4c9cb`. The boundary at which Conventional Commits 1.0 enforcement begins; commits at or after this SHA must conform, commits before are grandfathered as "pre-policy."

**Scope.** Commit-discipline enforcement (Core Rule 5.21 / 7.1); `CHANGELOG.md` categorization; `tests/test_commit_format.py`.

**Authoritative source.** Core Rule 5.21; commit_msg_hook.py (moved to slop-process private repo); `ms-changelog` `CUTOFF_SHA` constant.

**See also.** [Core Rule](#core-rule).

---

### Docker migration (RETIRED)

**Definition.** The bare phrase "Docker migration" is RETIRED from new writing. It was used in v4.x docs in two unrelated senses — (1) the containerization work that shipped with v4.0.0, and (2) a hypothetical future containerization phase. The collision was the motivating incident for this glossary.

**Authoritative source.** This glossary.

**Preferred form.** Use [v4.0.0 containerization](#v400-containerization) for the shipped work, or [future containerization](#future-containerization) for the deferred phase. Never bare "Docker migration."

**See also.** [v4.0.0 containerization](#v400-containerization), [future containerization](#future-containerization).

---

### drift

**Definition.** Informal divergence from a canonical or authoritative state. Three project-specific flavors each carry a dedicated enforcement response: terminology drift (this glossary), structural drift (Rule 5.24's anti-pattern registry), schema drift (Rule 5.19's migration discipline).

**Scope.** Cleanup and hardening discussions; numbered drift incidents (Drift 1, Drift 2, ...) referenced in `COMPLETION_AUDIT.md`-style audit docs.

**Authoritative source.** This glossary.

**See also.** [drift pattern](#drift-pattern), [structural enforcement](#structural-enforcement), [audit gate](#audit-gate).

---

### drift pattern

**Definition.** A typology of drift cases used in `HARDENING_V4_2_PLAN.md` to map enforcement work to incident classes:

- **Pattern A** — changes that span multiple files but aren't structurally required to land together. Motivated the rule-addition contract and the ledger linter.
- **Pattern B** — file-operation residue. Motivated the repository-structure linter.
- **Pattern C** — terminology drift across long-running plans. Motivated this glossary.
- **Pattern D** — per-run scratch with no expiry. Motivated the pytest `basetemp` setting and the 7-day cleanup.

**Scope.** `HARDENING_V4_2_PLAN.md` "DRIFT PATTERNS BEING ADDRESSED" section.

**Authoritative source.** `HARDENING_V4_2_PLAN.md`.

**See also.** [drift](#drift), [audit gate](#audit-gate).

---

### future containerization

**Definition.** The currently-deferred phase of further SLOP containerization, distinct from the v4.0.0 work that shipped. The pure-containerization option was evaluated 2026-05-11 and rejected; the hybrid model — SLOP on host, managed apps in Docker — is preserved. Any further work in this direction belongs to v5 planning.

**Scope.** v5 planning; `SESSION_CONTEXT.md` "Direction Decisions" section.

**Authoritative source.** `SESSION_CONTEXT.md` Direction Decisions; this glossary records the preferred phrasing.

**Preferred form.** "future containerization" or, when more specific, "v5 hybrid containerization."

**Do not confuse with.** [v4.0.0 containerization](#v400-containerization) (already shipped), bare [Docker migration](#docker-migration-retired) (RETIRED).

**See also.** [v4.0.0 containerization](#v400-containerization), [Docker migration (RETIRED)](#docker-migration-retired).

---

### hardening (v4.2)

**Definition.** The v4.2 phase of SLOP: extending the systematic-enforcement pattern from code-level rules (the cleanup arc) to the documentation and filesystem layer. Bookkeeping discipline plus structural enforcement, not new architectural rules.

**Scope.** The HARDENING_V4_2_PLAN.md (v4.x plan, moved to slop-process private repo) and its three tiers (filesystem hygiene, multi-document atomic enforcement, vocabulary discipline).

**Authoritative source.** HARDENING_V4_2_PLAN.md Premise (moved to slop-process private repo).

**Do not confuse with.** Generic "hardening" (security hardening, performance hardening). Bare "hardening" in v4.x docs refers to v4.2 unless qualified.

**See also.** [cleanup arc](#cleanup-arc), [bookkeeping discipline](#bookkeeping-discipline), [structural enforcement](#structural-enforcement).

---

### infra slot

**Definition.** A category of shared infrastructure resource managed by infra providers — port number, app key, slot identifier (`cls.slot`), compose fragment path. Any allocation must check availability before side effects (Core Rule 1.7).

**Scope.** `backend/infra/registry.py`, `backend/infra/providers/*.py`; Core Rule 1.7; ADR 0009.

**Authoritative source.** ADR 0009; Core Rule 1.7.

**See also.** [compose fragment](#compose-fragment--fragment), [manifest](#manifest).

---

### installer

**Definition.** The `installer/` Python package plus the `install.sh` bootstrap wrapper and the `slop` CLI subcommand suite. The bootstrap (`install.sh`) downloads the repo, sets up a virtualenv, and delegates to `installer/main.py`. The subcommand suite exposes `install`, `uninstall`, `purge`, `clean`, `status`, and `smoke` operations. The package namespace is `installer.*`; external callers (audit-gate tools) import from it directly rather than spawning subprocess invocations.

**Scope.** `install.sh`, `installer/`, `installer/main.py`; ADR 0013 §1; V5_INSTALLER_PLAN.md.

**Authoritative source.** ADR 0013; `V5_INSTALLER_PLAN.md` overview.

**Do not confuse with.** the `backend/` package (runtime service, served continuously after install). The installer runs once per install/uninstall/purge/clean operation; the backend runs persistently.

**See also.** [state file](#state-file), [smoke test (installer)](#smoke-test-installer), [verify_removed()](#verify_removed).

---

### INV-N framework

**Definition.** The audit-mode invariant naming scheme spanning the v5.0 ADRs: INV-1 through INV-6 in ADR 0013 (installer layout and state-file contract), INV-7 through INV-11 in ADR 0015 (smoke test contract and POST_INSTALL.txt lifecycle), INV-12 through INV-16 in ADR 0017 (uninstall semantics and label scheme). Each invariant row has an invariant column (what must hold), a verification column (how to check it mechanically), and an audit-gate column (which V5_INSTALLER_PLAN.md Step 4.5.a finding exercises it).

**Scope.** `docs/adr/0013*.md`, `docs/adr/0015*.md`, `docs/adr/0017*.md`; `V5_INSTALLER_PLAN.md` Step 4.5.a.

**Authoritative source.** The invariant tables in the respective ADRs.

**See also.** [U1 through U7](#u1-through-u7), [verify_removed()](#verify_removed), [audit gate](#audit-gate).

---

### ledger

**Definition.** The "RECORD OF COMPLETIONS" section at the bottom of `PROJECT_CLEANUP.md` and `HARDENING_V4_2_PLAN.md`, where each completed sub-task gets a one-line entry with date, commit hash, and summary. A **ledger gap** is a `[ ] → [x]` flip in the task list that lacks a corresponding ledger entry in the same commit.

**Scope.** The v4.x PROJECT_CLEANUP.md and HARDENING_V4_2_PLAN.md (moved to slop-process private repo), any v5+ plan adopting the same structure.

**Authoritative source.** The check_cleanup_ledger.py linter (moved to slop-process private repo); HARDENING_V4_2_PLAN.md Step 2.5 designed it.

**See also.** [companion change (C1–C5)](#companion-change-c1c5) (parallel concept for Core Rules), [rule-addition contract](#rule-addition-contract).

---

### manifest

**Definition.** A hand-authored catalog YAML file at `catalog/apps/<name>.yaml` describing a managed app — category, ports, `web_port`, `health.checks`, `traefik:` block. Validated by `TestCatalogCompliance` (Core Rule 5.5).

**Scope.** `catalog/apps/`; Core Rules 3.1–3.4, 5.5.

**Authoritative source.** `tests/test_non_catalog_installs.py::TestCatalogCompliance` and the catalog YAML schema implied by it.

**Do not confuse with.** [compose fragment](#compose-fragment--fragment) (generated output, not catalog input). "Manifest" in the project always means catalog input.

**See also.** [compose fragment](#compose-fragment--fragment), [infra slot](#infra-slot).

---

### slop.managed / slop.app-key

**Definition.** The two Docker label keys unconditionally applied to every slop-managed container and volume by `backend/core/compose.py::build_service_fragment` at compose-fragment write time:
- `slop.managed=true` — marks the resource for `purge` (U6/U7 enumeration) and `clean` (§C.2/§C.3 enumeration).
- `slop.app-key=<key>` — identifies the owning catalog app; used by `clean` for per-app fidelity and by `purge` to distinguish orphan containers.

The `slop.*` label namespace is project-reserved. Third-party labels in this namespace risk collision with purge/clean enumeration (ADR 0017 §D.4).

**Scope.** `backend/core/compose.py` (write); `installer/uninstall.py` (read during purge/clean); ADR 0017 §D; CITATIONS.md entry `LABEL-SCHEME`.

**Authoritative source.** ADR 0017 §D.1 label contract table.

**See also.** [U1 through U7](#u1-through-u7), [INV-N framework](#inv-n-framework), [compose fragment](#compose-fragment--fragment).

---

### model gate

**Definition.** The structural check, performed before beginning any sub-task, that compares the sub-task's `[OPUS]` / `[SONNET]` tag against the operator's current model. If they do not match, the operator stops and surfaces the mismatch; the operator cannot self-authorize past a mismatch. Explicit user override is required to proceed with a model other than the tagged one.

**Scope.** All sub-tasks in `PROJECT_CLEANUP`-style and `HARDENING`-style plans.

**Authoritative source.** `HARDENING_V4_2_PLAN.md` "MODEL GATE — HARD STOP BEFORE TASK BOUNDARIES" section.

**Do not confuse with.** [audit gate](#audit-gate), [pre-flight verification](#pre-flight-verification) — release-process checkpoints, not model assignment. Model gate is per-sub-task; the other two are per-release.

**See also.** [[OPUS] / [SONNET] tag](#opus--sonnet-tag), [operator](#operator), [protocol violation](#protocol-violation).

---

### ms-audit

**Definition.** The `ms-audit` tool. Runs a contract audit against project state every deploy and provides `--improve` mode that aggregates mutmut survivors, schemathesis failures, and keploy replay failures into an LLM context for AI-generated test suggestions.

**Scope.** Toolchain (`/srv/slop/ms-audit`); Core Rule 5.2 (enforcement layer); Core Rule 5.13 (AI context assembly).

**Authoritative source.** `ms-audit` source; Core Rules 5.2, 5.13.

**Do not confuse with.** [audit gate](#audit-gate) (release-process checkpoint, different mechanism), [audit (general)](#audit-general) (the broader concept of independent review).

**See also.** [audit (general)](#audit-general), [audit gate](#audit-gate).

---

### ms-test.py

**Definition.** Project-specific full-pipeline test runner at the repo root. Distinct from the standard `pytest` framework that runs `tests/*.py` files. `ms-test.py` orchestrates coverage scans, FSM exercises, integration checks, and audit hooks; `pytest` runs the unit and integration assertions written under `tests/`.

**Scope.** `/srv/slop/ms-test.py` and `ms-test-all`; cf. ADR 0007.

**Authoritative source.** ADR 0007 (`ms-test.py` vs `pytest`).

**Do not confuse with.** `pytest` (the standard framework). When a Core Rule cites `pytest tests/test_X.py`, it means the framework. When a doc cites `ms-test.py Q3`, it means the in-house runner's named check.

---

### platform

**Definition.** The per-instance SLOP state — the database row, configuration, and deployed-app inventory managed by `backend/platform/`. "Platform-not-configured" means the fresh-install state with no platform row; "platform reset" is the wizard-driven teardown-and-redo path.

**Scope.** `backend/platform/`; Core Rule 3.7 (zero-data state); Core Rule 1.5 (platform reset example).

**Authoritative source.** `backend/platform/` module; Core Rule 3.7.

**Do not confuse with.** "Platform" as marketing positioning. When unambiguous, "the platform" in code and ops contexts means the instance state.

**See also.** [wizard](#wizard).

---

### POST_INSTALL.txt

**Definition.** The post-install handoff artifact written by the installer at `<install_dir>/POST_INSTALL.txt` (default: `/opt/slop/POST_INSTALL.txt`). Written only on a successful install with a passing smoke test. Contains service URL, default credentials, first-steps commands, and support pointers. Owner `slop:slop`, mode 0644. Its presence is a file-system signal that the install was healthy (S2a); absence alongside a state file at `phase: installed` signals smoke-test failure (S2b). Not operator-editable; the installer is the sole writer.

**Scope.** `installer/` (write path); ADR 0015 §6 (contract); INV-8, INV-9.

**Authoritative source.** ADR 0015 §6.

**See also.** [state file](#state-file), [smoke test (installer)](#smoke-test-installer), [S2a / S2b](#s2a--s2b).

---

### pre-flight verification

**Definition.** Release-tag-time checklist run from the tag-creation prompt template. Includes structural audit clean (`HARDENING_V4_2_PLAN.md` Step 1.4.i), vocabulary sweep (Step 3.2.b), CI checks clean, pytest clean. Distinct from the audit gate (which produces a deliverable review doc).

**Scope.** The release process doc (moved to slop-process private repo) section 3.2; v4.x.x and later tag creation.

**Authoritative source.** The release process doc (moved to slop-process private repo) section 3.2; this glossary for canonical disambiguation.

**Do not confuse with.** [audit gate](#audit-gate), [model gate](#model-gate).

**See also.** [audit gate](#audit-gate).

---

### project boundary

**Definition.** The line between code defined inside `backend/` and dependencies outside it. Tests are allowed to mock at the [system boundary](#system-boundary) (subprocess, network, the docker_client wrapper, filesystem outside `tmp_path`); they should use real fakes rather than mocks of helpers defined within `backend/`.

**Scope.** Test code; Core Rule 4.12; ADR 0002.

**Authoritative source.** Core Rule 4.12; ADR 0002.

**Do not confuse with.** [system boundary](#system-boundary) — paired concept; opposite side of the line. Project boundary = "internal"; system boundary = "external."

**See also.** [system boundary](#system-boundary), [real fake](#real-fake).

---

### protocol violation

**Definition.** An operator behavior that breaks an explicit rule of conduct documented in `HARDENING_V4_2_PLAN.md` operator-protocol sections — e.g., silently bundling unmentioned changes into a commit, silently substituting a different deliverable, proceeding past a model-gate mismatch without explicit override, letting `SESSION_CONTEXT.md` stale by two or more commits. Surface; do not paper over.

**Scope.** All Claude Code sessions for SLOP v4.2+.

**Authoritative source.** `HARDENING_V4_2_PLAN.md` sections "MODEL GATE," "COMMIT MESSAGE / CONTENT ALIGNMENT," "SESSION_CONTEXT.md UPDATE CADENCE."

**See also.** [operator](#operator), [model gate](#model-gate).

---

### real fake

**Definition.** A genuine but isolated alternative to mocking a project-internal helper — a `tmp_path`-rooted compose directory instead of `patch("backend.manifests.executor.write_fragment")`; a real `StateDB(tmp_path / "state.db")` instead of `patch("backend.core.state.StateDB")`; a fixture-managed `Config(...)` with test paths instead of `patch("backend.core.config.config")`. The phrase is unusual enough that it merits explicit definition.

**Scope.** Test code; Core Rule 4.12.

**Authoritative source.** Core Rule 4.12; ADR 0002 (worked examples).

**See also.** [system boundary](#system-boundary), [project boundary](#project-boundary).

---

### release-tag gate

**Definition.** Same concept as [audit gate](#audit-gate); the two phrases are used interchangeably across project docs. "Release-tag gate" appears in Rule 5.24 wording; "audit gate" appears in casual usage and in `HARDENING_V4_2_PLAN.md` discussion of Tier 3.

**Scope.** Release process at v4.x.x and v5+ tag creation.

**Authoritative source.** This glossary; see [audit gate](#audit-gate) for the canonical entry.

**Preferred form.** [audit gate](#audit-gate) for conversational reference; "release-tag gate" when distinguishing from per-commit gates in formal Core Rule text.

**See also.** [audit gate](#audit-gate), [pre-flight verification](#pre-flight-verification).

---

### renumbering pattern (Core Rules)

**Definition.** The convention by which Core Rules planned for one section ship in another, leaving dual-reference aliases. Specifically: rules planned as Sections 6 / 7 / 8 / 9 (Migration / Commit / Complexity / Observability disciplines) were shipped under Section 5 (Process & Tooling) or Section 4 (Architectural Rules as Code) and carry both numbers — e.g., Core Rule 5.22 is also "Core Rule 8.1," Core Rule 5.19 is also "Core Rule 6.1."

**Scope.** `CORE_RULES.md` headings (formatted as `### 5.19 Migration Discipline (Core Rule 6.1)`); `CORE_RULES.md` Section 8 version history (which carries "Number is X.Y not the planned Z.W" notes).

**Authoritative source.** Core Rule version-history entries; the dual-numbered headings themselves.

**Aliases.** "planned vs as-shipped numbers"; "alt-numbered rules."

**Do not confuse with.** the C1 requirement of the rule-addition contract (heading-plus-entry parity, not section choice).

**See also.** [Core Rule](#core-rule).

---

### rule-addition contract

**Definition.** The set of five companion changes (C1–C5) that must land in the same commit as a new numeric Core Rule, enforced by `ms-rule-contract`. Designed in ADR 0012; codified as Core Rule 5.25.

**Scope.** Every commit adding a Core Rule.

**Authoritative source.** ADR 0012; Core Rule 5.25.

**See also.** [companion change (C1–C5)](#companion-change-c1c5), [Core Rule](#core-rule), [three-layer pattern](#three-layer-pattern).

---

### rule entry (ms-coverage)

**Definition.** An entry in the `ms-coverage` `RULES` list, structured as `id`, `label`, `rationale`, `risk` (`critical`/`high`/`medium`), `test_fn`, `test_file`. Every verifiable Core Rule has a paired rule entry; `ms-coverage` surfaces gaps where a rule's `test_fn` is missing from the test corpus.

**Scope.** `ms-coverage` source; coverage_map.json.

**Authoritative source.** Core Rule 4.1; `ms-coverage` source.

**Aliases.** **rule slug** refers to the kebab-case `id` field (e.g., `migration-discipline`, `rule-addition-contract`).

**Do not confuse with.** [Core Rule](#core-rule) (the narrative architectural rule), [structural anti-pattern rule](#structural-anti-pattern-rule) (the anti-pattern registry; moved to slop-process), [Semgrep rule](#semgrep-rule) (`.semgrep/rules/*.yml`).

**See also.** [Core Rule](#core-rule), [rule-addition contract](#rule-addition-contract).

---

### S2a / S2b

**Definition.** Two sub-states of the `S2` existing-install detection case (ADR 0013 §4 / ADR 0015 §7), distinguished by the `smoke_test_passed` field and `POST_INSTALL.txt` presence:
- **S2a — installed and ready:** `phase: "installed"`, `smoke_test_passed: true`, `POST_INSTALL.txt` present. Normal re-run refusal; `--force` reinstalls.
- **S2b — installed but smoke failed:** `phase: "installed"`, `smoke_test_passed: false`, `POST_INSTALL.txt` absent. Install pipeline completed but runtime readiness was not confirmed. The refusal message names the problem and offers `--force` reinstall or future standalone smoke-rerun (v5.1+).

**Scope.** `installer/main.py` (detection + refusal logic); ADR 0013 §4; ADR 0015 §7.

**Authoritative source.** ADR 0015 §7.

**See also.** [state file](#state-file), [smoke test (installer)](#smoke-test-installer), [POST_INSTALL.txt](#post_installtxt).

---

### Semgrep rule

**Definition.** A pattern-based architectural rule in `.semgrep/rules/core-rules.yml`, enforcing Core Rules at the syntactic level — e.g., `bare-db-commit`, `unsanitized-user-key-as-path`, `result-add-not-fail`. Run by `ms-semgrep` and the CI Semgrep gate.

**Scope.** `.semgrep/rules/`; Core Rule 5.15.

**Authoritative source.** Core Rule 5.15.

**Do not confuse with.** [Core Rule](#core-rule) (narrative), [rule entry (ms-coverage)](#rule-entry-ms-coverage) (machine-checkable companion to a Core Rule), [structural anti-pattern rule](#structural-anti-pattern-rule) (repository-structure registry).

**See also.** [Core Rule](#core-rule).

---

### smoke test (installer)

**Definition.** The installer's single first-run readiness verification, run at the end of `install.sh` after the install pipeline completes. Evaluates five predicates (P1–P5): P1 = service active under systemd, P2 = port in LISTEN state owned by the service PID, P3 = `/healthz` returns 200 JSON, P4 = `/readyz` returns 200 JSON, P5 = frontend bytes and QuickStart API served from `/`. Total wall-clock budget: 30 seconds. On pass: writes `smoke_test_passed: true` to the state file and writes `POST_INSTALL.txt`, then exits 0. On fail: leaves state at `smoke_test_passed: false` and exits nonzero with predicate-specific diagnostics.

**Scope.** `installer/smoke.py`; ADR 0015 §1–§5; INV-7, INV-8, INV-9.

**Authoritative source.** ADR 0015.

**Preferred form.** "installer smoke test" or "the smoke test" when context is unambiguous. Avoid bare "smoke test" in code or ADRs — the general QA usage is common and causes drift.

**Do not confuse with.** "smoke test" as a general QA term (shallow test run to check basic functionality before deeper testing). The installer smoke test is a *runtime readiness check* at deploy time, not a test-suite triage technique.

**See also.** [state file](#state-file), [POST_INSTALL.txt](#post_installtxt), [S2a / S2b](#s2a--s2b), [INV-N framework](#inv-n-framework).

---

### state file

**Definition.** The installer's canonical record of what it did: `<install_dir>/.installer-state.json` (default: `/opt/slop/.installer-state.json`). Written twice during install (pre-write with `phase: "installing"` immediately after creating the install dir; post-write with `phase: "installed"` after the pipeline completes) and a third time by the smoke test (`smoke_test_passed: true` on pass). Mode 0640 (installer-internal, not operator-editable). Every installer subcommand reads from it; it is the single source of truth for install location, phase, and smoke status. Written via temp-file-and-rename for atomicity.

**Scope.** `installer/state.py` (read/write); ADR 0013 §2 (schema and lifecycle); ADR 0015 §5 (smoke-test write).

**Authoritative source.** ADR 0013 §2.

**Do not confuse with.** `backend/core/state.py` (`StateDB` — the backend's SQLite operational state). The installer state file and `StateDB` are independent systems with different lifecycles and owners; Flake 5 is an instance of code that confused the two.

**See also.** [installer](#installer), [smoke test (installer)](#smoke-test-installer), [S2a / S2b](#s2a--s2b), [POST_INSTALL.txt](#post_installtxt).

---

### step (cleanup sub-division)

**Definition.** A numbered division of a cleanup or hardening tier in `PROJECT_CLEANUP.md` or `HARDENING_V4_2_PLAN.md` (e.g., "Step 1.1," "Step 3.1"). Each step has lettered sub-tasks (1.1.a, 1.1.b, ...) and is closed by a ledger entry after CI checks and pytest pass.

**Scope.** `docs/cleanup/*.md`.

**Authoritative source.** `PROJECT_CLEANUP.md` and `HARDENING_V4_2_PLAN.md` COMPLETION RULES sections.

**Preferred form.** When a sentence could ambiguously mean either kind of step, qualify: "cleanup step 3.1" vs "wizard step `step_persist_settings`."

**Do not confuse with.** [step (process function)](#step-process-function) — named functions in the wizard / install flow.

**See also.** [ledger](#ledger), [tier (cleanup or hardening phase)](#tier-cleanup-or-hardening-phase).

---

### step (process function)

**Definition.** A named function in the wizard or install flow that performs one phase of a multi-phase operation (e.g., `step_persist_settings`, `step_deploy_infra`, `step_traefik_deploy`). Each returns a `StepResult`; failures are tested via `StepResult` assertions, not exceptions.

**Scope.** `backend/platform/wizard.py`, `backend/manifests/executor.py`; FSM tests (Core Rule 5.3).

**Authoritative source.** Wizard and executor source; Core Rule 5.3.

**Preferred form.** Use the bare function name (`step_persist_settings`) when discussing a specific function; use "wizard step" or "install step" when discussing the category.

**Do not confuse with.** [step (cleanup sub-division)](#step-cleanup-sub-division) — the plan-document concept.

**See also.** [wizard](#wizard), [manifest](#manifest).

---

### strategy doc

**Definition.** A per-step planning doc at `docs/cleanup/STEP_<N>_<NAME>_STRATEGY.md`, authored at the [OPUS] stage before [SONNET] implementation. Describes *how* to implement the step — design choices, drop-in code, acceptance criteria. Sequenced; not durable.

**Scope.** All cleanup and hardening steps with both [OPUS] and [SONNET] sub-tasks.

**Authoritative source.** Core Rule 4.15 distinguishes strategy docs from ADRs ("strategy docs describe HOW; ADRs describe WHY").

**Do not confuse with.** [ADR](#adr-architecture-decision-record) — the durable why-record. When a strategy doc's design becomes a Core Rule, the ADR is the durable record; the strategy doc becomes historical.

**See also.** [ADR](#adr-architecture-decision-record), [[OPUS] / [SONNET] tag](#opus--sonnet-tag).

---

### structural anti-pattern rule

**Definition.** A rule in the anti-pattern registry, of form `(id, description, check_fn, remedy)`. Enforced via pre-commit hook (`--staged` mode, hard-block), `ms-update` post-deploy block (`--audit` mode, advisory), and the release-tag-gate checklist. The registry (check_structural_antipatterns.py) and rule catalogue (STRUCTURAL_RULES.md) were moved to the slop-process private repo; the checks run through CI.

**Scope.** CI anti-pattern checks; Core Rule 5.24.

**Authoritative source.** Core Rule 5.24; check_structural_antipatterns.py (moved to the slop-process private repo).

**Do not confuse with.** [Core Rule](#core-rule), [rule entry (ms-coverage)](#rule-entry-ms-coverage), [Semgrep rule](#semgrep-rule). The structural anti-pattern registry is specifically for repository-structure drift; the other rule families cover code-level invariants.

**See also.** [structural enforcement](#structural-enforcement), [Core Rule](#core-rule).

---

### structural enforcement

**Definition.** The architectural pattern of scanning staged changes, identifying structural triggers, requiring dependent changes to be present in the same commit, and blocking at commit time. Used by `ms-refactor` (refactoring contract), `ms-rule-contract` (rule-addition contract), check_structural_antipatterns.py (anti-pattern registry; moved to slop-process private repo), check_cleanup_ledger.py (ledger linter; moved to slop-process private repo). The shared mechanism is what makes the rule registries cheap to extend.

**Scope.** All `tools/check_*.py` linters and `ms-*` enforcement tools.

**Authoritative source.** `HARDENING_V4_2_PLAN.md` Premise; Core Rules 5.24 and 5.25 instantiate it.

**See also.** [rule-addition contract](#rule-addition-contract), [structural anti-pattern rule](#structural-anti-pattern-rule), [bookkeeping discipline](#bookkeeping-discipline).

---

### supported distro set / Shape B

**Definition.** The set of Linux distributions that `install.sh` tests against and officially supports. For v5.0.0 the set is **Ubuntu 24.04 LTS, Debian 13, Debian 12** — codified by ADR 0016 under the **Shape B** policy: "latest LTS enters at the `.1` point release." Under Shape B, Ubuntu 26.04 enters the supported set at v5.0.1 (August 2026, following the 26.04.1 Canonical readiness signal). Shape A (not chosen) would have added Ubuntu 26.04 immediately at v5.0.0 despite it being three weeks post-release. Ubuntu 22.04 was removed from the v5.0 supported set after Step 3.3 matrix testing surfaced production-blocking findings (SQLite version floor + `libsqlite3-dev` packaging divergence).

**Scope.** `install.sh` (distro guard); `installer/SUPPORTED_DISTROS.md`; ADR 0016; V5_INSTALLER_PLAN.md Step 3.3.

**Authoritative source.** ADR 0016.

**See also.** [INV-N framework](#inv-n-framework), [smoke test (installer)](#smoke-test-installer).

---

### system boundary

**Definition.** The line between code inside `backend/` and external dependencies — subprocess, network (`httpx`, `urllib`, `socket`), filesystem operations outside `tmp_path`, the `backend.core.docker_client` wrapper (treated as a boundary because it wraps `docker-py`), `/proc` readers. Mocking is allowed at this line; mocking internal-to-`backend/` helpers is discouraged in favor of real fakes.

**Scope.** Test code; Core Rule 4.12; ADR 0002.

**Authoritative source.** Core Rule 4.12; ADR 0002.

**Do not confuse with.** [project boundary](#project-boundary) — paired concept; opposite side of the line.

**See also.** [project boundary](#project-boundary), [real fake](#real-fake).

---

### three-layer pattern

**Definition.** The defense-in-depth pattern used by most v4.x enforcement: a pre-commit hook (fast; prevents the violation from landing) plus a pytest test (catches `--no-verify` bypass and rebase corruption) plus a CI or operator-driven check (catches the test-isn't-running case). Codified in Core Rule 7.1 / 5.21 (commit discipline) and applied throughout Rules 4.x and 5.x.

**Scope.** Every commit-enforced Core Rule that has both runtime and structural enforcement.

**Authoritative source.** Core Rule 5.21 (formal codification in commit-discipline context); Core Rules 5.22, 5.23, 5.24, 5.25 (subsequent applications).

**See also.** [Core Rule](#core-rule).

---

### tier (cleanup or hardening phase)

**Definition.** A numbered phase of a cleanup or hardening plan, grouping related steps (e.g., "TIER 1 — Foundational" in `PROJECT_CLEANUP.md`, "TIER 1 — Filesystem Hygiene" in `HARDENING_V4_2_PLAN.md`). Tiers are sequential; the next tier doesn't start until all steps in the current tier are ✓ DONE.

**Scope.** `docs/cleanup/*.md` plan documents.

**Authoritative source.** `PROJECT_CLEANUP.md` and `HARDENING_V4_2_PLAN.md` structure.

**Preferred form.** "cleanup Tier 2" or "v4.2 Tier 2" when context isn't obvious.

**Do not confuse with.** [tier (rate-limit category)](#tier-rate-limit-category) — another use of the same word.

**See also.** [step (cleanup sub-division)](#step-cleanup-sub-division), [cleanup arc](#cleanup-arc), [hardening (v4.2)](#hardening-v42).

---

### tier (rate-limit category)

**Definition.** A bucket in the rate-limiting tier table (Core Rule 4.14): heavy mutation (5/min — install/remove/replace, wizard run, platform reset), heavy read (10/min — LLM-triggering endpoints), light mutation (30/min — settings, registry, storage, routing), default (60/min — other GETs). Four buckets, applied via `@limiter.limit(...)` decorators in `backend/api/`.

**Scope.** `backend/api/rate_limit.py`; mutating endpoints under `backend/api/`; Core Rule 4.14; ADR 0004.

**Authoritative source.** Core Rule 4.14; ADR 0004.

**Preferred form.** Always qualified: "rate-limit tier."

**Do not confuse with.** [tier (cleanup or hardening phase)](#tier-cleanup-or-hardening-phase).

---

### U1 through U7

**Definition.** The seven removal-completeness predicates defined in ADR 0017 §B.2, evaluated by `verify_removed()` after `uninstall` or `purge`:
- **U1:** `systemctl is-active slop.service` returns `inactive` or `unknown`
- **U2:** `/etc/systemd/system/slop.service` does not exist
- **U3:** `<install_dir>` does not exist
- **U4:** `getent passwd slop` returns nonzero (subject to §A.6 pre-existing-user carve-out)
- **U4b:** `getent group slop` returns nonzero or group has no unexpected members (subject to §A.6.5 carve-out)
- **U5a:** data dir preserved and untouched — inode and mtime unchanged (*uninstall* only)
- **U5b:** `<data_dir>` does not exist (*purge* only)
- **U6:** `docker ps -a --filter label=slop.managed=true` returns empty (*purge* only)
- **U7:** `docker volume ls --filter label=slop.managed=true` returns empty (*purge* only)

U1–U3 failures stop the pipeline; U4, U4b, U5–U7 failures are reported and make the exit nonzero but the rest of the pipeline continues. `clean` has inverted polarity: U1, U2, U3 *violated* is expected (slop keeps running); U6 and U7 *hold* (managed containers/volumes removed).

**Scope.** `installer/uninstall.py`; ADR 0017 §B.2; INV-12, INV-13, INV-14.

**Authoritative source.** ADR 0017 §B.2.

**See also.** [verify_removed()](#verify_removed), [slop.managed / slop.app-key](#slopmanaged--slopapp-key), [INV-N framework](#inv-n-framework).

---

### v4.0.0 containerization

**Definition.** The Docker-based deployment work that shipped with the v4.0.0 tag — replaced the previous deployment model with docker-compose orchestration. Distinct from any further containerization work that might happen post-v4.2.

**Scope.** v4.0.0 tag annotation; `docs/MIGRATION.md`; `CHANGELOG.md` pre-policy entries; `ARCHITECTURE.md`.

**Authoritative source.** v4.0.0 tag annotation; `docs/MIGRATION.md`.

**Preferred form.** Use this phrase, not "Docker migration."

**Do not confuse with.** [future containerization](#future-containerization) (deferred / v5+).

**See also.** [future containerization](#future-containerization), [Docker migration (RETIRED)](#docker-migration-retired).

---

### verify_removed()

**Definition.** The pure function `installer/uninstall.py::verify_removed(install_dir, data_dir, mode)` that evaluates U-predicates against post-uninstall state and returns a structured result: `mode` (one of `'uninstall'`, `'purge'`, or `'clean'`), `success` (bool), `predicates` (dict mapping predicate name to bool), `skipped` (list of predicate names skipped per documented §A.6 / §A.6.5 carve-outs). The single point of audit-mode access — no code re-implements the predicate checks inline. The v5.0.0 audit gate consumes it via IA-4 (installer-importable, not a subprocess wrapper).

**Scope.** `installer/uninstall.py`; ADR 0017 §A.7; INV-12, INV-13, INV-14.

**Authoritative source.** ADR 0017 §A.7.

**See also.** [U1 through U7](#u1-through-u7), [INV-N framework](#inv-n-framework), [installer](#installer).

---

### wizard

**Definition.** The SLOP setup flow that bootstraps the platform from a fresh-install (zero-data) state — installs infra providers, configures domain and Traefik, deploys management apps, marks the platform configured. Implemented in `backend/platform/wizard.py`. "Wizard reset" is the teardown-and-rerun path; "wizard contracts" are the regression tests pinning the flow.

**Scope.** `backend/platform/wizard.py`; setup view (`frontend/src/views/SetupView.vue`); Core Rules 1.5, 1.6, 3.7.

**Authoritative source.** `backend/platform/wizard.py`.

**See also.** [platform](#platform), [step (process function)](#step-process-function).
