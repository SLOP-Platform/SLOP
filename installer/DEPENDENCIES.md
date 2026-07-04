# System Dependencies

SLOP's installer manages a small set of system packages on the host. This document defines which packages, where they come from, what versions are acceptable, and how the installer behaves when host state diverges from expectation. It is consulted by:

- `installer/deps_debian.py` (Tier 2.2 `ensure_dependencies()`): the implementation operates from this doc's matrix.
- `installer/docker.py` (Tier 2.2 `ensure_docker()`): consumes §Docker Handling.
- `install.sh` (Tier 1.3): the bootstrap dependencies (`python3`, `git`) are documented here for completeness; `install.sh` remains their canonical implementation.
- Operators answering "what does the installer touch on my host?"
- The v5.0.0 audit gate (V5_INSTALLER_PLAN.md Step 4.5): the audit verifies doc ↔ implementation parity per the invariants in §Audit Invariants.
- Future contributors adding new distro families (Fedora, RHEL, Arch in v5.1+): the per-distro structure here is the template.

The matrix is small at v5.0 (six packages across three distros). The template is precedent: every later distro family — `installer/deps_redhat.py`, `installer/deps_arch.py`, and so on — will mirror this structure. Adding Fedora 40 in v5.1 should be a copy-paste-edit operation, not a "rethink the template" exercise.

## Maintenance Contract

This doc is the canonical source for what the installer touches on the host. `installer/deps_debian.py::DEPENDENCIES` mirrors it. Five rules govern updates:

1. **Adding a dependency entry.** Edit this doc *and* `installer/deps_debian.py::DEPENDENCIES` in the same commit. Add fixtures to `installer/tests/test_deps_debian.py`: at minimum a present-and-OK case, a present-but-below-floor case, and an absent case. Doc is canonical; if doc and code disagree, doc wins and code is corrected.

2. **Removing a dependency entry.** Land removal in this doc, in `installer/deps_debian.py`, and in the test suite atomically. Removing a previously-installed dep does **not** cause the installer to remove it from the host on re-run; idempotency is one-directional (see §Idempotency Contract). Operator-side cleanup is the operator's responsibility post-uninstall.

3. **Bumping a minimum version.** Allowed but precedent-bearing. Requires an in-entry rationale note (upstream EOL, downstream feature need, codebase change requiring a feature only in newer versions). Update fixtures accordingly. Hosts on the previous floor will fail post-bump; this is a breaking change for those operators and must be called out in CHANGELOG.

4. **Tested versions.** Append-only. Versions land in the per-distro entries' Tested versions list when an audit (per V5_INSTALLER_PLAN.md Step 3.3 or its successors) exercises an end-to-end install on that version. Versions are never removed; the list documents the historical test record. Regressions are filed as bugs, not silent removals.

5. **Adding a distro family.** A new family (e.g. `redhat-derivatives` for Fedora 40+ in v5.1) adds: one new "Per-Distro Entry" subsection; one or more new columns to the Dependency Matrix; a new `installer/deps_<family>.py` module with the family's `DEPENDENCIES`; new fixtures in `installer/tests/test_deps_<family>.py`. The Global Constraints, Docker Handling, Install Ordering, Error Handling, Idempotency Contract, and Audit Invariants sections should generalize without amendment. If they require amendment for a new family, that is a signal those sections were under-specified and should be tightened in the same commit.

A structural anti-pattern rule enforcing this doc ↔ `deps_<family>.py` sync continuously (the analog of Core Rule 5.26 for paths) is deferred to v5.1, to be filed if drift is observed. Until then, the contract is procedural, audit-checked at Tier 4.5 via INV-D2, and PR-reviewed.

## Dependency Matrix

The matrix is the machine-readable contract. `installer/deps_debian.py::DEPENDENCIES` must agree with the Debian 12 / Ubuntu 22.04 / Ubuntu 24.04 columns for the four installer-managed rows.

| Package                          | Debian 12      | Ubuntu 22.04          | Ubuntu 24.04 | Minimum version  | Installed for          |
|----------------------------------|----------------|-----------------------|--------------|------------------|------------------------|
| `python3` + `python3-venv` †     | apt main       | deadsnakes PPA (3.11) | apt main     | 3.11             | Bootstrap (install.sh) |
| `git`                            | apt main       | apt main              | apt main     | (any in main)    | Bootstrap (install.sh) |
| `curl`                           | apt main       | apt main              | apt main     | (any in main)    | Installer (Tier 2.2)   |
| `netcat-openbsd`                 | apt main       | apt main              | apt main     | (any in main)    | Installer (Tier 2.2)   |
| `docker-ce` + `docker-compose-plugin` | get.docker.com | get.docker.com   | get.docker.com | 24.0 (engine) | Installer (Tier 2.2)   |
| `nodejs`                         | NodeSource 22.x | NodeSource 22.x      | NodeSource 22.x | 20.19         | Installer (Tier 2.2)   |

† `python3-venv` is a separate apt package on Debian-derived distros (the `python3` apt package does not include `venv` support on its own). `install.sh` installs both. On Ubuntu 22.04's deadsnakes path, the package names are `python3.11` and `python3.11-venv`, and `update-alternatives` re-points `python3` at `python3.11` system-wide (per `install.sh` step 5).

Six packages. Two installed by bootstrap (`install.sh`). Four installed by Tier 2.2 (`installer/deps_debian.py::ensure_dependencies()`). Docker is the only one whose source is a third-party convenience script; everything else uses apt with project-trusted sources.

## Global Constraints

These apply to every package and every distro; they are not repeated per-entry.

**apt invocation.** Package installs use `apt-get install -y -qq` (script mode, accept defaults, low verbosity). This matches `install.sh` bootstrap step 5 exactly. `--no-install-recommends` is *not* used: consistency with the bootstrap is worth more than the disk-footprint dividend on a single-tenant host. If a future operator runs the installer on a constrained host, that is v5.1+ work under its own ADR.

**`apt-get update` timing.** Run conditionally, once per installer invocation, only if at least one apt-source package needs installing. This matches `install.sh`'s `_needs_apt_update` gating: a no-op re-run (everything already present and acceptable) should not invoke `apt-get update` and should not block on network availability. The exception: NodeSource and Docker convenience scripts run their own `apt-get update` internally; the installer does not re-run it after.

**Network availability.** The installer assumes the host has working internet at install time. It must, to reach apt mirrors, NodeSource, Docker's convenience script, and the slop git repo. Network failures during dep install are diagnosable and recoverable (re-run resumes from where it failed; see §Idempotency Contract); they do not corrupt host state.

**Architecture.** v5.0 assumes x86_64. Production Python dependencies in `requirements.txt` all have manylinux x86_64 wheels available on PyPI; the installer therefore does **not** install build tooling (no `gcc`, no `python3-dev`, no `libffi-dev`). If a wheel is unavailable, pip falls back to source compilation and fails loudly — that is the contract, and the correct failure mode (loud, recoverable, no invisible 150MB of build tools dragged in). ARM64 is not detection-blocked (per `installer/SUPPORTED_DISTROS.md` Global Constraints) but is unaudited; an ARM64 install may require build tooling that v5.0 does not provide. ARM64 audit is deferred.

**Pre-existing Python environments.** The installer uses whatever `python3` resolves to as root on the host. If the operator runs `sudo ./install.sh` from a shell where pyenv, conda, or asdf has shimmed `python3` into PATH, the shim is what the installer will use. In practice this rarely happens — those tools are per-user and root shells don't inherit them unless the operator deliberately exports them — but the contract is explicit: the installer is agnostic to host-level Python managers. The `.venv` it creates at `<install_dir>/.venv` is the only Python environment slop actually runs against; the system `python3` is only the bootstrap interpreter that creates that venv. Hosts with non-distro Python *can* work; if they break, the remediation is "run the installer with a clean PATH or temporarily disable the manager."

**Idempotency direction.** The installer adds packages; it does not remove them, downgrade them, or alter their apt pinning. Once a package is on the host (whether installer-installed or pre-existing), the installer's behavior on re-run is verify-and-skip for acceptable versions. The single exception is Docker D3 (present but below the 24.0 floor), where consent-gated upgrade applies. See §Idempotency Contract for the full state table.

**No slop-specific apt pinning.** The installer does not write to `/etc/apt/preferences.d/` and does not hold any packages. NodeSource and Docker convenience scripts write their own apt sources (`/etc/apt/sources.list.d/nodesource.list` and `/etc/apt/sources.list.d/docker.list` respectively); the installer leaves those as the upstream scripts install them and does not augment them. The audit gate verifies these files exist post-install (INV-D3, INV-D4) but does not verify their contents — those belong to the upstream scripts, not to slop.

**Root execution.** All apt invocations and convenience-script invocations require root. `installer/prereq.py::_check_root` gates the entire installer on euid 0 before any dep install runs.

## Install Ordering

Order matters in three places only. The rest is independent and can be implemented in any sequence.

1. **`apt-get update`** runs first, conditionally, if at least one apt-source dep needs installing. Skip the call entirely if every apt-source dep is already present-and-acceptable.

2. **NodeSource setup script** must run before `apt-get install -y -qq nodejs`. The setup script writes `/etc/apt/sources.list.d/nodesource.list` and installs the NodeSource signing key; without it, `apt-get install nodejs` resolves to whatever Node version (if any) the distro's main repo ships — which on every supported distro is below the 20.19 floor. NodeSource's `setup_22.x` script runs `apt-get update` internally on first invocation, so the subsequent `apt-get install nodejs` sees the new repo.

3. **Docker installation** (when `--install-docker=yes` and Docker is absent, or D3 with consent — see §Docker Handling) runs `curl -fsSL https://get.docker.com | sh`. This must precede the user-provisioning step (Tier 2.6 `installer/user.py::ensure_user()`) because that step adds the `slop` user to the `docker` group, which must exist. ADR 0013 §5's `DockerGroupMissingError` is raised if the group is missing at user-provisioning time; ordering Docker install before user provisioning ensures that error is unreachable in normal flow.

`curl` and `netcat-openbsd` have no ordering dependencies among themselves or with the above. They are installed in the same `apt-get install` invocation as `nodejs` to minimize the number of apt transactions: a single `apt-get install -y -qq curl netcat-openbsd nodejs` covers all three (after NodeSource setup). If any of the three are present-and-acceptable, they are dropped from the argument list before the call — apt accepts already-installed packages in its argument list, but dropping them keeps the install log unambiguous about what was actually installed.

Bootstrap-managed deps (`python3`, `git`) are installed earlier, in `install.sh` steps 1–5, before `main.py` is `exec`'d. Their ordering relative to each other is `install.sh`'s concern and is not re-specified here.

## Docker Handling

Per ADR 0013 §3 (detect-and-prompt with consent flag), extended here to handle the version-floor case. The 24.0 floor comes from `backend/platform/wizard.py::step_docker_check`, which rejects Docker < 24.0 at first-run with `"Docker {version} is too old — requires 24.0+."`. The installer surfaces the same constraint at install time, before the wizard does, with consent-aligned semantics.

### Host-state detection

`installer/docker.py::ensure_docker(consent_mode)` classifies the host into one of four states:

| #  | Host state                                  | Detection signal                                                                 | Behavior                                                            |
|----|---------------------------------------------|----------------------------------------------------------------------------------|---------------------------------------------------------------------|
| D1 | Docker absent                               | `command -v docker` returns non-zero                                             | Consent-gated install. See §Consent resolution.                     |
| D2 | Docker present, version ≥ 24.0              | `docker version --format '{{.Server.Version}}'` exits 0 and parses to major ≥ 24 | No-op. Verified.                                                    |
| D3 | Docker present, version < 24.0              | Same query, parses to major < 24                                                 | Treated as absent for consent purposes. See §Consent resolution.    |
| D4 | Docker installed but daemon unreachable     | `docker version` exits non-zero (e.g. socket missing, daemon stopped)            | Fail-fast with `DockerDaemonError`; see §Error Handling.            |

D3 is the new case relative to ADR 0013 §3, which contemplated only "present vs absent." Treating D3 as "absent for consent purposes" preserves the operator-consent semantics from §3 uniformly: the operator who said `--install-docker=no` told the installer not to touch Docker, and that decision applies whether Docker is absent or merely too old.

### Consent resolution

For D1 and D3 (both requiring an install or upgrade), behavior depends on the consent flag and the TTY mode established by `install.sh` step 2 (ADR 0013 §3).

| `--install-docker` | TTY mode    | D1 (absent)                                                | D3 (too old)                                                                            |
|--------------------|-------------|------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| `=yes`             | any         | Install via get.docker.com.                                | Upgrade via get.docker.com. The convenience script handles in-place upgrade.            |
| `=no`              | any         | Fail-fast: `DockerMissingError` with remediation.          | Fail-fast: `DockerTooOldError` naming the detected version and the 24.0 floor.          |
| (unset)            | interactive | Prompt: "Docker is not installed. Install via get.docker.com? [y/N]" | Prompt: "Docker {version} is installed but slop requires 24.0+. Upgrade via get.docker.com? [y/N]" |
| (unset)            | pipe        | Already rejected at `install.sh` step 3 — unreachable here. | Same — unreachable.                                                                     |

The pipe-mode-with-unset-flag case is unreachable in `ensure_docker()` by design: `install.sh` step 3 (ADR 0013 §3) fails-fast before Python is `exec`'d. By the time `ensure_docker()` runs, either `--install-docker` is set or stdin is a TTY. The table includes the unreachable cells for completeness; the implementation does not need code paths for them.

### Convenience-script invocation

The headline invocation, identical in D1 and D3:

```bash
curl -fsSL https://get.docker.com | sh
```

Pinned to no specific Docker version. The official convenience script installs current stable Docker Engine, which has been past 24.0 since the Docker 24.0.0 release (2023-05). The installer does not pin a Docker version because the upstream script is the authoritative source for "current stable" and re-pinning would be a maintenance burden with no benefit — Docker engine minor releases are backward-compatible within the 24.x+ band the wizard accepts.

Post-install verification (run unconditionally after the script returns 0):

1. `docker version --format '{{.Server.Version}}'` exits 0 (D4 contract satisfied).
2. The engine version parses and its major is ≥ 24 (D2/D3 contract satisfied).
3. `docker compose version --short` exits 0 (the compose v2 plugin is installed and discoverable). The convenience script installs `docker-compose-plugin` automatically on all supported distros; this is the verification that it succeeded.

If any post-install check fails, the installer raises `DockerInstallFailedError` naming the failing check, and prints `journalctl -u docker --no-pager -n 50` as the recommended diagnostic command.

### Rootless Docker

Not supported. ADR 0013 §5 documents the docker-group-membership-is-root-equivalent trade-off; that decision implies rootful Docker. If `command -v docker` happens to resolve to a rootless install (typically operator-level, with `~/.docker/cli-plugins/docker-compose` and a user-namespace daemon), the installer's detection still classifies the host into D1–D4 based on version reachability, but the `slop` user's `docker` group membership (Tier 2.6) will not grant access to the rootless daemon. This is a known limitation; an operator with rootless Docker is on an unsupported path. Reconsidering rootless support is v5.1+ work per ADR 0013 §5's security note.

### Compose plugin version

The compose plugin (`docker compose`, space-separated, v2 syntax) is installed as part of `docker-compose-plugin` by the convenience script. The backend uses `docker compose` exclusively; there is no fallback to `docker-compose` (v1, hyphenated, Python package). The installer does not version-pin the compose plugin separately: any `docker-ce` 24.0+ from get.docker.com ships with a compose plugin recent enough for the backend's usage (engine ↔ plugin compatibility is upstream's responsibility within the 24.x+ band).

## Per-Distro Entries

Each entry names the source for each dep and any distro-specific quirks. The matrix above is the machine-readable spec; these entries add prose and rationale. Within each entry, deps are labeled (bootstrap) or (installer) to indicate whether `install.sh` or `deps_debian.py` is the authoritative implementation site.

### Debian 12 (Bookworm) and forward-compatible

- **`python3`** *(bootstrap)*: apt main, currently 3.11.2 (satisfies the 3.11 floor). `install.sh` installs `python3` and `python3-venv` as a single apt-get-install. No PPA needed; Debian 12 ships 3.11 in main.
- **`git`** *(bootstrap)*: apt main, currently 2.39.5. No floor enforced (any version in main suffices).
- **`curl`** *(installer)*: apt main, currently 7.88.1. **Not installed by default on Debian 12 minimal** (the netinstall and debian-installer minimal variants); will be installed by the installer if absent. Default-on for the `standard` task selection in debian-installer.
- **`netcat-openbsd`** *(installer)*: apt main, currently 1.219. Default-on for the `standard` task selection. The installer typically verifies-and-skips on Debian 12 server installs; installs if absent.
- **`docker-ce` + `docker-compose-plugin`** *(installer)*: get.docker.com. The convenience script detects Debian, configures `download.docker.com/linux/debian` as the apt source with the appropriate codename, installs the apt signing key, and runs `apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`. The installer does not interact with the script's internals.
- **`nodejs`** *(installer)*: NodeSource setup script for 22.x (`setup_22.x`). Apt main has Node 18.x, which fails the 20.19 floor; NodeSource is required.
- **Known quirks**: None.
- **Tested versions**: *(none yet; v5.0.0 audit targets Debian 12 per V5_INSTALLER_PLAN.md Step 3.3)*
- **EOL reference**: https://endoflife.date/debian (Debian 12 LTS support through 2028-06).

### Ubuntu 22.04 (Jammy LTS)

- **`python3`** *(bootstrap)*: **deadsnakes PPA** (`ppa:deadsnakes/ppa`), installing `python3.11` and `python3.11-venv`. Apt main on 22.04 has Python 3.10, which fails the 3.11 floor. `install.sh` runs `apt-get install -y software-properties-common`, then `add-apt-repository -y ppa:deadsnakes/ppa`, then `apt-get update`, then `apt-get install -y python3.11 python3.11-venv`, then `update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1` so that `python3` resolves to 3.11 system-wide. The `update-alternatives` mutation is a global side effect: an operator who maintains custom alternatives slots for `python3` will see those slots re-prioritized. This cost is documented in `INSTALL.md` (Tier 4.4) as a host-state change.
- **`git`** *(bootstrap)*: apt main, currently 2.34.1. Same contract as Debian 12.
- **`curl`** *(installer)*: apt main, currently 7.81.0. **Installed by default on Ubuntu 22.04 Server**; the installer will typically verify-and-skip.
- **`netcat-openbsd`** *(installer)*: apt main, currently 1.218. Installed by default on Ubuntu 22.04 Server.
- **`docker-ce` + plugin** *(installer)*: get.docker.com. The convenience script detects Ubuntu 22.04 and configures `download.docker.com/linux/ubuntu` with codename `jammy`.
- **`nodejs`** *(installer)*: NodeSource `setup_22.x`. Apt main on 22.04 ships Node 12.x (which is multiple years EOL); NodeSource is mandatory.
- **Known quirks**: The deadsnakes `update-alternatives` mutation is the only host-state change in this entry that has scope beyond slop's own paths. Operators who maintain custom `update-alternatives` priorities for `python3` should re-check `update-alternatives --display python3` post-install. v5.1 may revisit this (a self-contained Python via `uv`, PEP 668-compatible venvs without system-Python promotion); v5.0 keeps the established path because the alternative would require rewriting `install.sh` step 5 and the F1 fix from the Tier 1 audit.
- **Tested versions**: *(none yet; v5.0.0 audit targets Ubuntu 22.04 per V5_INSTALLER_PLAN.md Step 3.3)*
- **EOL reference**: https://endoflife.date/ubuntu (Jammy standard support through 2027-04, ESM through 2032-04).

### Ubuntu 24.04 (Noble LTS)

- **`python3`** *(bootstrap)*: apt main, currently 3.12.3. Satisfies the 3.11 floor (3.12 ≥ 3.11). `install.sh` installs `python3` and `python3-venv` directly — no PPA, no `update-alternatives` mutation.
- **`git`** *(bootstrap)*: apt main, currently 2.43.0.
- **`curl`** *(installer)*: apt main, currently 8.5.0. Default on Ubuntu 24.04 Server.
- **`netcat-openbsd`** *(installer)*: apt main, currently 1.226. Default on Ubuntu 24.04 Server.
- **`docker-ce` + plugin** *(installer)*: get.docker.com (codename `noble`).
- **`nodejs`** *(installer)*: NodeSource `setup_22.x`. Apt main on 24.04 has Node 18.x — close to the floor but below; NodeSource is still required.
- **Known quirks**: None. Ubuntu 24.04 is the cleanest of the three supported distros from a deps-install perspective: no PPA, no alternatives promotion, every dep is one apt invocation away from satisfied.
- **Tested versions**: *(none yet; v5.0.0 audit targets Ubuntu 24.04 per V5_INSTALLER_PLAN.md Step 3.3)*
- **EOL reference**: https://endoflife.date/ubuntu (Noble standard support through 2029-04, ESM through 2034-04).

### v5.1+ forward look (informational, not normative)

The structure above generalizes for v5.1 distro additions:

- **Fedora 40+** (proposed for v5.1): adds a Fedora column to the Dependency Matrix and a "Fedora 40" entry below. Source columns change: `dnf` instead of `apt`; NodeSource provides a Fedora-targeted `setup_22.x` setup script; Docker's convenience script handles Fedora natively.
- **RHEL / Rocky / AlmaLinux 9+**: similar to Fedora; `dnf`; subscription/repo activation may add a step before package install.
- **Arch Linux**: `pacman`; rolling-release means no version-floor in the apt sense; Docker via `pacman -S docker docker-buildx`; Node via `nodejs-lts-jod` (the Node 22 LTS package as of v5.0 timing).

The Global Constraints, Install Ordering, Docker Handling, Error Handling, Idempotency Contract, and Audit Invariants sections should generalize without amendment. If a v5.1 family breaks any of those generalizations, the affected section is under-specified and should be tightened in the same commit that adds the family (per Maintenance Contract rule 5).

## Error Handling

Each failure mode has a detection signal, an operator-facing message template, and a remediation hint. Messages are quoted verbatim where the wording is load-bearing; placeholder values use `{angle-brackets}`.

### Package not found (apt source not configured)

- **Detection**: `apt-get install -y -qq <pkg>` returns exit 100 with stderr matching `E: Unable to locate package`.
- **Operator message**: `apt cannot find package '{pkg}'. The required apt source may not be configured. Re-run the installer with --verbose to see the full apt output, or check /etc/apt/sources.list and /etc/apt/sources.list.d/ for the expected source.`
- **Remediation**: For `nodejs` not found, the likely cause is the NodeSource setup script failing silently (no network, key install rejected); re-running the installer retries the setup. For `curl`, `netcat-openbsd`, or `python3` not found in main, the host's apt sources are misconfigured at the distro level — operator runs `apt-cache search <pkg>` to confirm; remediation is operator-level.

### Network failure during `apt-get update`

- **Detection**: `apt-get update` returns non-zero with stderr matching `Failed to fetch`, `Could not resolve`, or `Connection timed out`.
- **Operator message**: `apt-get update failed: network unreachable or apt mirror unavailable. The installer requires internet access to install packages. Check connectivity (try: ping deb.debian.org) and re-run.`
- **Remediation**: Operator-level. Re-running the installer resumes from this step; the failed `apt-get update` has no side effects on host state.

### Network failure during NodeSource setup

- **Detection**: `curl -fsSL https://deb.nodesource.com/setup_22.x | bash -` returns non-zero, OR the post-setup `apt-get install nodejs` reports `nodejs` as unavailable.
- **Operator message**: `NodeSource setup script failed. slop requires Node.js 20.19+ which is not available in {distro}'s apt main. Manual install instructions: https://github.com/nodesource/distributions`
- **Remediation**: Operator-level (network, DNS, or firewall). Re-running the installer resumes. Manual NodeSource setup is documented as the supported fallback.

### Network failure during get.docker.com

- **Detection**: `curl -fsSL https://get.docker.com | sh` returns non-zero, OR post-script `command -v docker` returns non-zero, OR post-script `docker version` fails.
- **Operator message**: `Docker installation via get.docker.com failed. See output above for the failing step. Manual install: https://docs.docker.com/engine/install/{distro}/`
- **Remediation**: Operator may install Docker manually (e.g. via apt with the official `download.docker.com` apt source) and re-run the installer with `--install-docker=no`. The re-run detects Docker as present (D2 if version-acceptable, D3 otherwise) and proceeds.

### Version conflict (existing package below floor, refuses to upgrade silently)

- **Docker D3**: handled by consent resolution (§Docker Handling). Three deterministic paths depending on `--install-docker` value and TTY mode; no silent behavior.
- **`nodejs`**: NodeSource `setup_22.x` followed by `apt-get install -y -qq nodejs` upgrades an existing `nodejs` package to the NodeSource 22.x version. apt handles the transaction. If apt refuses (e.g. a held package), the operator sees apt's verbatim error and remediates at the apt level (`apt-mark unhold nodejs`, etc.).
- **`python3` on bootstrap**: `install.sh` step 5 checks `python3 -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)"` for ≥ 311; if below, the deadsnakes path on Ubuntu 22.04 installs `python3.11` alongside the system Python 3.10 and re-points `python3` via `update-alternatives`. There is no auto-upgrade of an existing `python3` from main; the deadsnakes parallel install is the answer.

### apt lock held by another process

- **Detection**: `apt-get install` (or `apt-get update`) returns exit 100 with stderr matching `Could not get lock /var/lib/dpkg/lock` (or `lock-frontend`).
- **Operator message**: `apt is currently in use by another process. This is typically unattended-upgrades running its periodic update on a fresh Ubuntu install. Wait for it to finish (it usually takes 1-5 minutes) and re-run the installer.`
- **Remediation**: Wait, then re-run. The installer's pre-write state file (`phase: installing`, ADR 0013 §2) is the recovery anchor: the re-run interprets the host as "previous install incomplete" (S3 per ADR 0013 §4) and `--force` may be required if the prior partial run already created the install directory (configured via `--install-dir`). Per the ADR 0013 §4 S3 path, this resumes cleanly.

### Docker installed but daemon not running

- **Detection**: D4 in §Docker Handling.
- **Operator message**: `Docker is installed but the daemon is not reachable. Start it with 'systemctl start docker' and re-run the installer.`
- **Remediation**: Operator-level.

### Permission denied during convenience script

- **Detection**: `get.docker.com` requires root; `prereq.py::_check_root` catches non-root invocation earlier in the install pipeline, but if a regression causes it to slip past, `curl ... | sh` will fail at the apt invocation inside the script.
- **Operator message**: This case should be unreachable. If observed, the bug is in `prereq.py::_check_root` or its placement in `main.py`'s install pipeline; file as a bug rather than treating it as an operator-actionable error.

### Dependency version unparseable

- **Detection**: For deps with a floor (Docker engine, nodejs), the post-install check parses the version string. If parsing fails (e.g. an unexpected suffix, a missing newline, an empty stdout), the installer raises `DependencyVersionUnparseableError`.
- **Operator message**: `Could not parse {dep} version from output: '{raw_output}'. This is unusual; please file a bug at https://github.com/Nnyan/SLOP/issues with the full installer log.`
- **Remediation**: Bug-class, not operator-actionable. The installer fails-fast rather than continuing on an unverified install.

## Idempotency Contract

The installer's behavior on re-run is unambiguous: every dependency check produces one of four outcomes, and the action for each is fixed.

| Host state                                          | Action                                                                                  |
|-----------------------------------------------------|-----------------------------------------------------------------------------------------|
| Absent                                              | Install (via the source named in §Dependency Matrix).                                   |
| Present, version satisfies floor                    | Verify and skip. No apt invocation. One-line "OK" per dep in the log.                   |
| Present, version below floor                        | Fail-fast for installer-managed apt deps; consent-gated upgrade for Docker D3.          |
| Present, version unparseable                        | Fail-fast with `DependencyVersionUnparseableError`, naming the dep and raw version.     |

Rules:

- The installer **never downgrades** any package.
- The installer **never upgrades** apt-source deps that are already present and above-floor. Example: if `nodejs` 22.5.0 is installed and the floor is 20.19, the installer does not bump to a newer 22.x. The exception is Docker D3, which is consent-gated upgrade.
- The installer **never removes** packages. Uninstall (Tier 4.1, ADR 0014) removes slop's own paths and the `slop` user; system packages stay.
- A re-run that completes without installing anything is a successful no-op. It emits one-line "OK" per dep (six lines total from the deps step) and exits normally.
- A re-run after a failed previous run resumes from the failed step. There is no partial-install state to repair *within* the deps step: each dep check is independent, and apt's transaction semantics make partial-install impossible for apt-source deps. The get.docker.com path is the only one with non-apt semantics, and its idempotency is the upstream script's — running it on an already-installed Docker is a no-op modulo an `apt-get update`.

The pre-write state file (`phase: installing`, ADR 0013 §2) is the cross-step anchor for the install pipeline as a whole (between deps install and user provisioning, between user provisioning and repo fetch, and so on). Within the deps install step, the per-dep idempotency above is the anchor — no cross-dep state is recorded; the state file does not enumerate which deps the installer touched.

## Audit Invariants

These are mechanically checkable on a working v5 install. They are the deps-specific equivalent of ADR 0013's INV-1 through INV-6 and are verified by the v5.0.0 audit gate (V5_INSTALLER_PLAN.md Step 4.5.a).

| #      | Invariant                                                                              | Verification                                                                                                                                  | Audit-gate finding                                              |
|--------|----------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------|
| INV-D1 | Every dep in §Dependency Matrix is present on the host post-install.                   | For each dep, the audit runs `installer/deps_debian.py::verify(<dep>)`, a pure-read check (e.g. `command -v <cmd>`, `dpkg -s <pkg>`). All return OK. | Step 4.5.a finding 1 (install from clean VM works).             |
| INV-D2 | DEPENDENCIES.md ↔ `installer/deps_debian.py::DEPENDENCIES` field-by-field parity.       | Audit parses this doc's Dependency Matrix table and compares against the `DEPENDENCIES` constant. Mismatch (missing dep, wrong source, wrong floor) fails the audit. | Step 4.5.a finding 7 (structural audit clean).                   |
| INV-D3 | NodeSource apt source is configured post-install.                                       | `test -f /etc/apt/sources.list.d/nodesource.list` AND the file's `deb` line contains `setup_22.x` or `node_22.x`.                              | Step 4.5.a finding 1.                                            |
| INV-D4 | Docker apt source is configured post-install (only if `--install-docker=yes` was used). | `test -f /etc/apt/sources.list.d/docker.list` AND its codename (`bookworm`, `jammy`, `noble`) matches the host distro. Skipped if `--install-docker=no` and Docker was pre-installed via a different mechanism. | Step 4.5.a finding 1.                                            |
| INV-D5 | Docker engine version ≥ 24.0 post-install.                                              | `docker version --format '{{.Server.Version}}'`, parse major, assert ≥ 24.                                                                    | Step 4.5.a finding 1.                                            |
| INV-D6 | `nodejs` version ≥ 20.19 post-install.                                                  | `node --version` (returns `v{major}.{minor}.{patch}`), parse and compare (major,minor) ≥ (20,19).                                              | Step 4.5.a finding 1.                                            |
| INV-D7 | Re-run installs nothing.                                                                | Audit-mode check: after a successful install, snapshot `dpkg-query -W` and `docker version`; re-run `install.sh`; compare. No package versions or installed-package-list change. | Step 4.5.a finding 4 (idempotent re-run produces no errors).     |

INV-D2 is the mechanical-parity check that makes drift between this doc and the implementation detectable at audit time. A structural anti-pattern rule for continuous enforcement (analog of Rule 5.26 for paths) is deferred to v5.1; until then, INV-D2 is the periodic check.

## Alternatives Considered

**Matrix-only spec without `installer/deps_debian.py::DEPENDENCIES`.** Considered. The constant is small (six entries, a few fields each) and could live as a literal in `ensure_dependencies()`. Rejected because INV-D2's mechanical-parity check needs a structured target; parsing the markdown matrix at audit time is fragile and ties the audit to markdown-table format choices. A code-side constant gives the audit a stable parse target and gives the implementation a single source of truth.

**Single `installer/deps.py` with all distros in one dict instead of per-family files.** Considered. Rejected. Per-family files make adding a v5.1 distro a pure new-file operation; a sprawling-dict approach would make the file a bottleneck on every distro addition and conflate distros with different package managers in the same module. The two-family-at-v5.0 scope does not justify the consolidation. (The plan's "Deferred to v5.1" already names `installer/deps_<family>.py` as the shape.)

**`--no-install-recommends` for tighter footprint.** Considered. Rejected for v5.0. Consistency with `install.sh` bootstrap (which does not use the flag) is worth more than the disk-footprint dividend on a single-tenant host. v5.1 may revisit under an "install profiles" feature.

**Pin a specific Docker engine version (e.g. 27.0.0) rather than "latest stable via get.docker.com".** Considered. Rejected. The upstream convenience script is the authoritative source for "current stable"; pinning duplicates that and adds maintenance burden (bumping the pin on every Docker minor). The floor (24.0) is the load-bearing number; the ceiling is open.

**Auto-upgrade Docker when present but < 24.0, without consent.** Considered. Rejected. An operator who said `--install-docker=no`, or who answered "no" at the interactive prompt, told the installer not to touch Docker. Silently upgrading violates that. The consent-aligned model (D3 treated as absent for consent purposes) keeps the rules from ADR 0013 §3 uniform across "absent" and "too old".

**Add Docker version-floor check to `prereq.py` so the failure happens before any side effect.** Considered. Rejected for v5.0. `prereq.py` is currently distro-agnostic (kernel, disk, ports, root, systemd); adding Docker version logic would duplicate the 24.0 floor across `prereq.py` and `ensure_docker()`, repeating the audit risk R5 pattern (knowledge duplicated across two enforcement sites). v5.0 accepts the slightly-worse failure UX — fail at the deps install step rather than at the prereq step — in exchange for keeping the floor in one place. v5.1 may revisit if real-world operator feedback shows the late failure is friction.

**Install build tooling (`gcc`, `python3-dev`, `libffi-dev`) defensively in case a Python wheel is unavailable.** Considered. Rejected. `requirements.txt` is small and every package has manylinux x86_64 wheels on PyPI; pip will not fall back to source compilation on x86_64. Installing build tooling preemptively would add ~150MB of packages the installer does not actually need. If a wheel disappears upstream, pip fails loudly and the operator can install `build-essential` themselves — the correct failure mode is loud and recoverable, not bundled invisibly.

**Use `apt` vs `apt-get`.** `apt-get` is the scripted-use interface; `apt` is interactive-first per its own man page warnings about unstable output for scripts. `install.sh` uses `apt-get`; the deps module matches.

**Document `nfs-common` as a conditional dep (used by `backend/platform/storage.py` for NFS mount setup).** Considered. Rejected for v5.0. NFS mount is an optional backend feature; slop core operates without it. Adding `nfs-common` to the unconditional install list would install a network filesystem client most operators do not use. The backend's `storage.py` surfaces `sudo apt-get install -y nfs-common` as a user-facing instruction string when the operator configures an NFS mount; that is the correct boundary — optional features stay optional and the operator installs their support packages on demand.

**Treat `curl` as "should already be present" and skip installing it.** Considered. `curl` is default on Ubuntu Server but **not** on Debian 12 minimal installs (per official Debian images). Skipping would create a hidden runtime failure mode on Debian: the wizard's IP-probe call would fail with `curl: command not found` only after install completes and the operator opens the wizard. Installing unconditionally costs ~0.5MB on Debian and is a verify-and-skip no-op on Ubuntu; the cost is trivial and the failure mode is eliminated.

**Target Node 24 LTS instead of Node 22 LTS.** Considered. Node 24 became Active LTS in October 2025 and is supported through April 2028 — a longer support window than Node 22 (Maintenance LTS until April 2027). Rejected for v5.0: Vite 8's release notes explicitly call out "20.19+, 22.12+" as the supported range, naming 22 explicitly; Node 22 has had ~18 months of LTS production soak by v5.0 ship while Node 24 has had ~7 months; the support window on Node 22 (~1 year remaining as of v5.0) comfortably exceeds v5.0's expected support life given that v5.1 is on the roadmap and could bump the NodeSource line. A v5.0.x patch could switch `setup_22.x` to `setup_24.x` if a real-world issue surfaces, with a one-line change in `installer/deps_debian.py`.

**Target `setup_lts.x` instead of pinning to `setup_22.x`.** Considered. NodeSource's `setup_lts.x` always points to the current LTS, which would auto-upgrade Node across major versions over time. Rejected. Auto-upgrading Node across majors is exactly the kind of silent system mutation the installer otherwise avoids (see the Docker D3 consent decision). Pinning the major in the setup script URL makes the major a deliberate choice that requires a CHANGELOG entry to bump.

## Dependency-version policy

This section governs how Python package versions are expressed in `requirements.txt` and
`pyproject.toml` and how the committed `uv.lock` is managed.

### Intent vs. resolution

- **`requirements.txt`** declares **intent**: the range of versions SLOP supports. Every
  entry uses floor-only `>=` by default. The floor is the version that shipped with the
  commit that added or last updated the entry (verified against the resolved `uv.lock` at
  that time). Floors are bumped deliberately, not speculatively.

- **`uv.lock`** declares **resolution**: the exact versions chosen for a consistent,
  reproducible install. It is committed to the repository. All production installs and CI
  runs derive from the lockfile. The lockfile is the reproducibility guarantee; `requirements.txt`
  alone is not.

### Cap discipline

Upper caps (`<N`) are discouraged. Add a cap only when a specific incompatibility is
documented. Every cap must be accompanied by a one-line comment naming what breaks above
it. Example:

```
# Pydantic — cap at <3: v3 removes v1-compat shims used by manifest validators.
pydantic>=2.10.4,<3
```

Undocumented caps accumulate silently and block security updates. When in doubt, use
floor-only and let the lockfile hold the resolution.

### Bumping floors and caps

- **Minor/patch bump**: update the floor in `requirements.txt` and `pyproject.toml`, run
  `uv lock`, commit both files. No special review required.
- **Major-version bump**: always requires a written justification entry in this section
  naming the package, the old and new major, and any breaking changes reviewed. The bump
  lands in a dedicated dependency-refresh commit, not bundled with feature work.
- **Removing a cap**: requires verifying the breaking-change rationale is resolved. Update
  this section to note the resolution and the version where it was cleared.

### Dependency-refresh train

Routine version bumps (minor/patch, transitive updates) land together on a periodic
periodic dependency-refresh pass. Ad-hoc bumps are allowed for security
fixes. The lockfile must always be regenerated and committed in the same commit as the
`requirements.txt` / `pyproject.toml` change.

### Audit tooling

`pip-audit` runs against the committed `uv.lock` and surfaces transitive CVEs. A clean
`pip-audit` result against `uv.lock` is the security posture signal — not a clean scan of
`requirements.txt` alone, which misses transitive deps. CVE triage follows the
`AUTONOMOUS-DEFAULTS.md` "dependency / lockfile" category for Robot-mode runs.
