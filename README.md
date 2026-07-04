# S.L.O.P. — Self hosted Linux Orchestration Platform

One-command install that turns a fresh Ubuntu or Debian server into a running self-hosted platform with a web UI and a catalog of 50+ apps to deploy.

See [docs/MAP.md](docs/MAP.md) for the full documentation index.

## What is this?

Run one command, get a working service on port 8080. Point your browser at it, walk through the setup wizard, then deploy self-hosted apps from the built-in catalog — Jellyfin, Sonarr, Radarr, Immich, Vaultwarden, Ollama, and more — without touching Docker manually. S.L.O.P. handles the container lifecycle, keeps things labeled and tracked, and gives you clean uninstall/purge commands when you want to start over.

The technical runtime uses `slop` as its package name (systemd unit: `slop.service`, install dir: `/opt/slop`). This is normal — the marketing name and the package name are separate things.

## System requirements

- **OS:** Ubuntu 24.04 LTS, Debian 13 (Trixie), or Debian 12 (Bookworm)
- **Architecture:** x86_64 only — ARM (Raspberry Pi, Oracle Ampere) is not yet supported
- **Memory:** 2 GB minimum, 4 GB recommended
- **Disk:** 10 GB free minimum
- **Docker:** 24.0+ with compose plugin (installer can handle this with `--install-docker=yes`)
- **Privileges:** requires `sudo` — the installer creates a system user and manages systemd

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Nnyan/SLOP/main/install.sh | sudo bash -s -- --install-docker=yes
```

Or from a local checkout:

```bash
git clone https://github.com/Nnyan/SLOP.git
cd SLOP
sudo ./install.sh --install-docker=yes
```

After install, the URL, login info, and operator commands are written to `/opt/slop/POST_INSTALL.txt`.

## App catalog (sample)

50+ apps available. A few highlights:

| Category | Apps |
|---|---|
| Media server | Jellyfin, Plex, Emby |
| Download / arr | Sonarr, Radarr, Lidarr, Readarr, Prowlarr, SABnzbd |
| Photos | Immich |
| AI / LLM | Ollama, LocalAI, llama.cpp server |
| Notes / docs | Affine, Memos, SilverBullet |
| Password manager | Vaultwarden |
| Files / sync | Syncthing, FileBrowser |
| Home | Mealie (recipes), Actual Budget, Vikunja (tasks) |
| Monitoring | Dozzle, Netdata, Beszel |

Full list in [`catalog/apps/`](catalog/apps/).

## Lifecycle commands

After install, `slop` is available at `/opt/slop/bin/slop`:

```bash
# Check status
systemctl status slop.service

# Uninstall (keeps your data dir)
sudo /opt/slop/bin/slop uninstall --yes

# Purge (removes everything including data and managed containers)
sudo /opt/slop/bin/slop purge --yes
```

> **Warning:** `slop purge` removes every Docker container labeled `slop.managed=true`. Do not apply this label to containers you manage outside of S.L.O.P.

## Supported distros

Verified on Ubuntu 24.04, Debian 13, Debian 12 (x86_64). Ubuntu 22.04 is not supported. ARM64 is deferred to a future release.

## Links

- [Install guide](INSTALL.md)
- [Release notes v5.0.0](docs/RELEASE_NOTES_v5_0_0.md)
- [License](LICENSE)
