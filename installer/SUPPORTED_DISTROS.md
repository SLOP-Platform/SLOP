# Supported Distributions

slop v5.0.0 supports the following Linux distributions:

| Distro | Version | Codename | Python | SQLite |
|---|---|---|---|---|
| Ubuntu | 24.04 LTS | Noble Numbat | 3.12 (main) | 3.45.1 |
| Debian | 13 | Trixie | 3.13 (main) | 3.46.1 |
| Debian | 12 | Bookworm | 3.11 (main) | 3.40.1 |

All versions listed are from the distribution's main archive — no third-party PPAs or non-default repositories are required.

**Architecture:** x86_64 only for v5.0.0. ARM64 is deferred.

**Policy:** The supported set follows the "latest LTS + R-1 per distro family" policy defined in ADR 0016.
See `docs/adr/0016-supported-distro-set.md` for the rationale, the Ubuntu 22.04 removal findings,
and the Ubuntu 26.04 deferral decision.

## Removed in v5.0.0

| Distro | Version | Reason |
|---|---|---|
| Ubuntu | 22.04 LTS (Jammy) | SQLite 3.37.2 lacks `unixepoch()` (backend fails at startup); deadsnakes + `update-alternatives` breaks APT's `apt_pkg` hook. See ADR 0016 §2. |

## Deferred (v5.0.1)

| Distro | Version | Target |
|---|---|---|
| Ubuntu | 26.04 LTS | v5.0.1 — after Ubuntu 26.04.1 ships (August 6, 2026), which opens the `do-release-upgrade` path from 24.04. |

## Runtime detection

`installer/detect.py::detect_os()` reads `/etc/os-release` and raises `UnsupportedDistroError`
if the host does not match a supported entry. The distro guard in `install.sh` is the first check;
`detect_os()` is the second check inside the Python installer.

Adding a new distro requires: updating `detect.py::SUPPORTED_DISTROS`, updating this file,
and landing evidence from a full end-to-end matrix row (per V5_INSTALLER_PLAN.md Step 3.3 pattern).
New distro additions route through ADR 0016 revision per §4.
