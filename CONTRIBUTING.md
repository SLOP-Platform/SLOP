# Contributing to SLOP

This document covers the conventions that aren't already enforced by CI. For the full architectural rule set, see [`docs/CORE_RULES.md`](docs/CORE_RULES.md).

## Pre-merge checklist

- [ ] All CI checks pass locally (run `pre-commit run --all-files` and `pytest`).
- [ ] New behaviour has a test (Core Rule 2.5: regression test in the same commit as the bug fix; Core Rule 4.11: snapshot test for stable outputs).
- [ ] `mypy --strict` clean for any backend file you touched (Core Rule 5.20).
- [ ] Commit subjects follow Conventional Commits 1.0 (Core Rule 7.1) — the commit-msg hook rejects malformed subjects.
- [ ] If you introduced a new architectural constraint, codify it in `ms-coverage` as a rule entry (Core Rule 4.1).
- [ ] Any example domain in docs/comments/test fixtures is an **RFC 2606 reserved** name — never a real registrable domain (see "Example domains in docs & comments" below).

## Example domains in docs & comments

For any placeholder domain in documentation, code comments, docstrings, or test fixtures, use a **reserved pseudo-TLD** name — i.e. one ending in `.example`, `.invalid`, `.test`, or `.localhost` (RFC 2606 / RFC 6761). For example: `myhost.example`, `api.invalid`.

The public-publish gate `check_public_output` flags any domain on a **real TLD** (`.com`, `.net`, `.io`, …) that is not in the private publish allow-list, and turns the **Public Pipeline Gates RED** at publish time — the scrubber cannot tell an illustrative real domain from a genuine private-host leak. The reserved pseudo-TLDs above are **not real TLDs**, so the gate skips them and the convention alone is sufficient (no separate per-instance gate — CLAUDE.md §6 anti-accretion).

Note the `example.<real-tld>` family (the RFC 2606 second-level reserved names) sits on a real TLD, so it passes the gate **only** if the operator has added it to the `allow:` › `domains:` list in `publish.identity.yaml`; prefer the pseudo-TLD forms above, which need no operator action. This recurred once as a real bug (an illustrative real domain in a docstring, #1127); the pseudo-TLD forms make it impossible.

> This section deliberately writes **no** real-TLD example domain — doing so would itself be the bug it warns against.

## Snapshot tests (Core Rule 4.11)

Outputs that downstream tools or operators depend on (CLI banners, machine-readable JSON, API response shapes) are pinned by `syrupy` snapshot tests in `tests/test_snapshots.py`. The committed snapshots live at `tests/__snapshots__/test_snapshots.ambr`.

### When a snapshot test fails

1. **Read the diff.** Snapshot failures show the old vs new output. If the change is unintended, fix the source.

2. **If the change is intentional, regenerate:**

    ```bash
    pytest tests/test_snapshots.py --snapshot-update
    ```

3. **Review the snapshot diff:**

    ```bash
    git diff tests/__snapshots__/
    ```

4. **Commit the snapshot update in the SAME commit as the source change** so reviewers see both halves of the contract together. A `feat(api):` that changes a response shape WITHOUT the matching snapshot update will be caught by CI.

### What gets a snapshot

In scope:
- API response shapes (frontend depends on stable keys)
- CLI banners and operator output
- Machine-readable JSON from `ms-*` tools
- Cross-session continuity outputs (e.g. `ms-status --handoff`)

Out of scope:
- Per-test outputs that vary by environment (use unit-test assertions instead)
- Compose fragment / manifest YAML (already covered by schema tests)
- Vue frontend visual rendering (would need Playwright + image diff; tracked separately)

See [`docs/cleanup/STEP_2_1_SNAPSHOT_STRATEGY.md`](docs/cleanup/STEP_2_1_SNAPSHOT_STRATEGY.md) for the full target list and rationale.

## Test independence (Core Rule 4.16)

See [docs/CORE_RULES.md §4.16](docs/CORE_RULES.md) for the full Test Independence Discipline. The CI gate is informational (`continue-on-error: true`) until the order-dependent backlog (tracked in [`docs/TODO_2026_05_08_test_independence_backlog.md`](docs/TODO_2026_05_08_test_independence_backlog.md)) clears.

## Commit conventions (Core Rule 5.21)

See [docs/CORE_RULES.md §5.21](docs/CORE_RULES.md) for the full Commit Discipline. Quick reference:

```
type(scope): subject ≤ 100 chars, no trailing period      (Conventional Commits 1.0)
#NNNN[ qualifier]?: subject ≤ 100 chars, no trailing period   (ticket-ref form)
```

Where:
- `type` ∈ feat | fix | refactor | perf | test | docs | chore | ci | style | revert | build
- `scope` is lowercase; comma permitted for multi-ref scopes (#1276). Uppercase scope rejected.
- **ticket-ref form** (`#1209`): `#` + number, optional space-led qualifier, then `: subject` — first-class alongside Conventional Commits, not a fallback.
- The commit-msg hook (`tools/commit_msg_hook.py`) rejects non-conforming subjects.

## Architecture Decision Records (Core Rule 4.15)

Architectural decisions that constrain the codebase live in `docs/adr/` as numbered Markdown files. The format is Context / Decision / Consequences / Status — see `docs/adr/template.md`. Existing examples:

- [`0001-database-migrations.md`](docs/adr/0001-database-migrations.md) — custom numbered-file migrations vs Alembic
- [`0002-mocking-policy.md`](docs/adr/0002-mocking-policy.md) — system boundary vs project boundary mocking
- [`0003-structured-logging-correlation-ids.md`](docs/adr/0003-structured-logging-correlation-ids.md) — structlog + ProcessorFormatter
- [`0004-rate-limiting-tiers.md`](docs/adr/0004-rate-limiting-tiers.md) — slowapi tier definitions

When you make an architectural decision (library choice, threshold, exception clause, enforcement mechanism), write an ADR in the same PR as the implementation. The ADR doesn't replace strategy docs (`docs/cleanup/STEP_*_STRATEGY.md`) — strategy docs describe HOW to implement; ADRs describe WHY this approach was chosen, durably.

ADR numbers are immutable once accepted. If a decision is later superseded, the old ADR stays in the directory with its `Status:` updated to `Superseded` and a `Supersedes:` link in the new ADR.

`tests/test_adr_discipline.py` enforces the convention — sequential numbering, four required sections, valid status value.

## Working with the cleanup steps

The Tier 1+2 cleanup work is sequenced in [`docs/cleanup/PROJECT_CLEANUP.md`](docs/cleanup/PROJECT_CLEANUP.md). Per-step strategy docs (`docs/cleanup/STEP_<N>_<NAME>_STRATEGY.md`) author OPUS-level decisions before SONNET-level implementation. When a step has both [OPUS] and [SONNET] sub-tasks, the OPUS strategy doc lands first.

The `ms-status` tool reports current step + sub-task progress. `ms-status --handoff` emits a session-start prompt that tells the next agent (human or otherwise) what to verify before assuming the previous session's claims.

## Agent handoff conventions

Cross-session work follows [`docs/HANDOFF_PROTOCOL.md`](docs/HANDOFF_PROTOCOL.md). The defining rule: *the new session does not trust prior summaries; it verifies via `git log` and the CI checks directly.* This protects against drift in long, multi-session work threads.
