# SLOP — Project Map

A flat reference of the top-level files and directories in a fresh clone.

## Root files

| Path | Description |
|---|---|
| `README.md` | Project overview and quick-start |
| `CONTRIBUTING.md` | Contributor workflow and conventions |
| `CHANGELOG.md` | Version history |
| `MIGRATION.md` | Migration notes between major versions |
| `install.sh` | Primary installer script (production installs) |
| `deploy.sh` | Service deployment and update script |
| `Makefile` | Common development tasks |
| `Dockerfile` | Container image definition |
| `docker-compose.yml` | Production compose definition |
| `docker-compose.dev.yml` | Development compose override |
| `docker-entrypoint.sh` | Container entrypoint |
| `pyproject.toml` | Python project metadata and tool configuration |
| `setup.cfg` | Additional Python package configuration |
| `requirements.txt` | Runtime Python dependencies |
| `requirements-dev.txt` | Development-only Python dependencies |
| `uv.lock` | Locked dependency manifest |
| `mypy.ini` | Mypy type-checking configuration |
| `ruff.toml` | Ruff linter/formatter configuration |
| `hypothesis.toml` | Hypothesis property-based test settings |
| `ms-audit` | Audit helper |
| `ms-check` | Quick check runner |
| `ms-update` | Service update tool |
| `ms-test.py` | Test runner entry point |
| `ms-test-all` | Full test suite runner |
| `ms-setup-tools` | Developer tooling setup |
| `ms` | Top-level CLI entry point |
| `slop-reality-probe` | Deployment health probe |

## Top-level directories

| Path | Description |
|---|---|
| `backend/` | FastAPI application — API, core logic, manifests, health, platform wizard |
| `frontend/` | Vue 3 SPA — the operator-facing web UI |
| `installer/` | Python installer package (`install.sh` delegates here) |
| `catalog/` | App catalog definitions (app manifests and metadata) |
| `cli/` | CLI implementation |
| `migrations/` | Database migration scripts |
| `tests/` | Test suite (pytest) |
| `tools/` | Developer and enforcement helper scripts |
| `docs/` | Project documentation (see below) |

## docs/ sub-tree

| Path | Description |
|---|---|
| `docs/INSTALL.md` | Installation and configuration reference |
| `docs/DEPLOY.md` | Deployment and update runbook |
| `docs/DOCKER_INSTALL.md` | Docker-based installation guide |
| `docs/MIGRATION.md` | Migration notes (also at root) |
| `docs/GLOSSARY.md` | Term definitions |
| `docs/BACKLOG.md` | Tracked work items |
| `docs/SANCTIONED-OPS-LOG.md` | Audit log of sanctioned operational changes |
| `docs/observability.md` | Metrics and logging reference |
| `docs/RELEASE_NOTES_v5_0_0.md` | v5.0.0 release notes |
| `docs/lessons.json` | Structured lessons learned |
| `docs/adr/` | Architecture Decision Records (ADR 0001–present) |
