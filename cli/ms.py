#!/usr/bin/env python3
"""ms — SLOP command-line interface.

Usage:
  ms status                      Platform and stack overview
  ms apps list                   List installed apps
  ms apps install <key>          Install an app from the catalog
  ms apps remove  <key>          Remove an installed app
  ms apps disable <key>          Disable an app gracefully
  ms apps enable  <key>          Re-enable a disabled app
  ms apps logs    <key>          Tail container logs (last 100 lines)
  ms apps restart <key>          Restart a running container
  ms catalog [search]            Browse available apps
  ms health                      Run a health cycle and show results
  ms health status               Last health cycle summary
  ms infra                       Show infrastructure slot status
  ms routing                     Show media type routing config
  ms wizard                      Run the platform setup wizard (interactive)

Config:
  API URL: set MS_URL env var or --url flag (default http://localhost:8080)
  Output:  set MS_NO_COLOR=1 to disable ANSI colors

Examples:
  ms status
  ms apps install sonarr
  ms catalog arr
  ms health
  ms --url http://192.168.1.100:8080 apps list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from typing import Any

# ── ANSI colors ────────────────────────────────────────────────────────────

NO_COLOR = os.environ.get("MS_NO_COLOR") or not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str) -> str:    return _c("31", t)
def cyan(t: str) -> str:   return _c("36", t)
def bold(t: str) -> str:   return _c("1",  t)
def dim(t: str) -> str:    return _c("2",  t)


# ── API client ─────────────────────────────────────────────────────────────


class APIError(Exception):
    pass


_ALLOWED_SCHEMES = ("http://", "https://")


class APIClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        if not any(self.base.startswith(s) for s in _ALLOWED_SCHEMES):
            raise APIError(f"Unsupported URL scheme (must be http or https): {self.base}")

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._req("POST", path, body)

    def put(self, path: str, body: dict | None = None) -> Any:
        return self._req("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self._req("DELETE", path)

    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base}/api{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 — scheme validated in __init__
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — scheme validated in __init__
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                detail = json.loads(raw).get("detail", raw)
            except Exception:
                detail = raw
            raise APIError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise APIError(
                f"Cannot reach SLOP at {self.base}.\n"
                f"  Is the server running? ({e.reason})\n"
                f"  Set MS_URL or use --url to override."
            ) from e


# ── Output helpers ─────────────────────────────────────────────────────────


def _table(headers: list[str], rows: list[list[str]], col_sep: str = "  ") -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header_line = col_sep.join(bold(h.ljust(widths[i])) for i, h in enumerate(headers))
    print(header_line)
    print(dim("─" * (sum(widths) + len(col_sep) * (len(headers) - 1))))
    for row in rows:
        print(col_sep.join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))


def _status_dot(status: str) -> str:
    return {
        "running":   green("●"),
        "healthy":   green("●"),
        "ok":        green("●"),
        "installing": cyan("◌"),
        "disabled":  dim("○"),
        "error":     red("●"),
        "unhealthy": red("●"),
        "warning":   yellow("●"),
    }.get(status, dim("○"))


def _ok(msg: str) -> None:
    print(f"{green('✓')} {msg}")


def _err(msg: str) -> None:
    print(f"{red('✗')} {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"{yellow('!')} {msg}")


# ── Commands ───────────────────────────────────────────────────────────────


def cmd_status(api: APIClient, _args: argparse.Namespace) -> int:
    platform = api.get("/platform/status")
    status = platform.get("status", "unknown")

    print()
    print(bold("  SLOP v3"))
    color_fn = green if status == "ready" else yellow
    print(f"  Platform  {color_fn(status)}")
    if platform.get("domain"):
        print(f"  Domain    {cyan(platform['domain'])}")
    if platform.get("network_name"):
        print(f"  Network   {dim(platform['network_name'])}")
    print()

    # Health scheduler
    try:
        sched = api.get("/health/scheduler")
        running = sched.get("running", False)
        last = sched.get("last_cycle_summary") or {}
        print(bold("  Health Scheduler"))
        print(f"  Status    {'running' if running else red('stopped')}")
        if last:
            checked = last.get("apps_checked", 0)
            healthy = last.get("apps_healthy", 0)
            degraded = last.get("apps_degraded", 0)
            color = green if degraded == 0 else yellow
            print(f"  Last run  {color(f'{healthy}/{checked} healthy')}"
                  + (f", {red(str(degraded) + ' degraded')}" if degraded else ""))
        print()
    except APIError:
        pass

    # Installed apps
    try:
        apps = api.get("/apps")
        running_apps = [a for a in apps if a.get("status") == "running"]
        degraded_apps = [a for a in apps if a.get("status") in ("error", "unhealthy")]
        disabled_apps = [a for a in apps if a.get("status") == "disabled"]
        print(bold("  Apps"))
        print(f"  Installed {len(apps)} total — "
              f"{green(str(len(running_apps)) + ' running')}"
              + (f", {yellow(str(len(disabled_apps)) + ' disabled')}" if disabled_apps else "")
              + (f", {red(str(len(degraded_apps)) + ' degraded')}" if degraded_apps else ""))
    except APIError:
        pass

    print()
    return 0


def cmd_apps_list(api: APIClient, _args: argparse.Namespace) -> int:
    apps = api.get("/apps")
    if not apps:
        print(dim("  No apps installed."))
        return 0

    rows = []
    for a in sorted(apps, key=lambda x: x.get("display_name", "")):
        status = a.get("status", "unknown")
        port = str(a.get("host_port") or "—")
        rows.append([
            _status_dot(status) + " " + a.get("display_name", a["key"]),
            a.get("key", ""),
            a.get("category", ""),
            port,
            status,
        ])

    print()
    _table(["App", "Key", "Category", "Port", "Status"], rows)
    print()
    return 0


def cmd_apps_install(api: APIClient, args: argparse.Namespace) -> int:
    key = args.key
    print(f"Installing {bold(key)}…")
    result = api.post(f"/apps/{key}/install", {})

    if result.get("installing"):
        print(f"  {cyan('◌')} Running in background — polling for progress…")
        import time
        seen_steps: set[int] = set()
        deadline = time.monotonic() + 600  # 10-minute overall timeout
        while time.monotonic() < deadline:
            time.sleep(0.8)
            progress = api.get(f"/apps/{key}/install/progress")
            for i, step in enumerate(progress.get("steps", [])):
                if i not in seen_steps:
                    seen_steps.add(i)
                    dot = _status_dot(step.get("status", ""))
                    print(f"  {dot} {step.get('message', '')}")

            if progress.get("done"):
                if progress.get("ok"):
                    print()
                    _ok(f"{key} installed successfully.")
                else:
                    _err(progress.get("error", "Installation failed."))
                    return 1
                return 0
        _err("Install timed out after 10 minutes. Check server: ms apps list")
        return 1
    else:
        _err(f"Unexpected response: {result}")
        return 1


def cmd_apps_remove(api: APIClient, args: argparse.Namespace) -> int:
    key = args.key
    keep = not getattr(args, "delete_config", False)
    print(f"Removing {bold(key)}{'(keeping config)' if keep else ''}…")
    result = api.delete(f"/apps/{key}")
    if result.get("ok"):
        _ok(f"{key} removed.")
    else:
        for s in result.get("steps", []):
            if s.get("status") == "error":
                _err(s.get("message", ""))
    return 0


def cmd_apps_disable(api: APIClient, args: argparse.Namespace) -> int:
    key = args.key
    result = api.post(f"/apps/{key}/disable", {"reason": "user_request"})
    if result.get("ok") is not False:
        _ok(f"{bold(key)} disabled.")
    else:
        _err(str(result))
    return 0


def cmd_apps_enable(api: APIClient, args: argparse.Namespace) -> int:
    key = args.key
    result = api.post(f"/apps/{key}/enable")
    if result.get("ok") is not False:
        _ok(f"{bold(key)} enabled.")
    return 0


def cmd_apps_restart(api: APIClient, args: argparse.Namespace) -> int:
    key = args.key
    api.post(f"/apps/{key}/restart")
    _ok(f"{bold(key)} restarted.")
    return 0


def cmd_apps_logs(api: APIClient, args: argparse.Namespace) -> int:
    key = args.key
    tail = getattr(args, "tail", 100)
    data = api.get(f"/apps/{key}/logs?tail={tail}")
    logs = data.get("logs", "")
    if not logs:
        print(dim("  No log output."))
    else:
        print(logs)
    return 0


def cmd_catalog(api: APIClient, args: argparse.Namespace) -> int:
    search = getattr(args, "search", None) or ""
    catalog = api.get("/catalog")

    rows = []
    for category, entries in sorted(catalog.items()):
        for app in entries:
            name = app.get("display_name", app["key"])
            if search and search.lower() not in name.lower() \
                    and search.lower() not in (app.get("description") or "").lower() \
                    and search.lower() not in " ".join(app.get("tags", [])).lower():
                continue
            rows.append([
                app.get("icon", "📦") + " " + name,
                app["key"],
                category,
                str(app.get("web_port") or "—"),
                " ".join(app.get("tags", [])[:3]),
            ])

    if not rows:
        print(dim(f"  No apps match '{search}'."))
        return 0

    print()
    _table(["App", "Key", "Category", "Port", "Tags"], rows)
    print(dim(f"\n  {len(rows)} apps — install with: ms apps install <key>"))
    print()
    return 0


def cmd_health(api: APIClient, _args: argparse.Namespace) -> int:
    print("Running health cycle…")
    api.post("/health/run")
    return cmd_health_status(api, _args)


def cmd_health_status(api: APIClient, _args: argparse.Namespace) -> int:
    try:
        checks = api.get("/health/apps")
    except APIError:
        checks = []

    if not checks:
        print(dim("  No health data yet. Run: ms health"))
        return 0

    by_app: dict[str, list[dict]] = {}
    for c in checks:
        by_app.setdefault(c["app_key"], []).append(c)

    print()
    any_error = False
    for app_key, app_checks in sorted(by_app.items()):
        worst = "ok"
        for c in app_checks:
            if c["status"] == "error":
                worst = "error"
            elif c["status"] == "warning" and worst != "error":
                worst = "warning"
        dot = _status_dot(worst)
        if worst in ("error", "warning"):
            any_error = True
        print(f"  {dot} {bold(app_key)}")
        for c in app_checks:
            cdot = _status_dot(c["status"])
            print(f"    {cdot} {c['check_name']}: {dim(c['summary'])}")

    print()
    return 1 if any_error else 0


def cmd_infra(api: APIClient, _args: argparse.Namespace) -> int:
    slots = api.get("/infra/slots")
    print()
    rows = []
    for s in slots:
        status = s.get("status", "empty")
        dot = green("●") if status == "active" else dim("○")
        provider = s.get("display_name") or s.get("provider") or "—"
        rows.append([dot + " " + s["slot"].capitalize(), provider, status])
    _table(["Slot", "Provider", "Status"], rows)
    print()
    return 0


def cmd_routing(api: APIClient, _args: argparse.Namespace) -> int:
    routes = api.get("/routing/media")
    print()
    rows = []
    for r in routes:
        t = r["media_type"]
        debrid = r.get("debrid_instance") or dim("—")
        download = r.get("download_instance") or dim("—")
        path = r.get("default_path", "download")
        path_str = cyan(path) if path == "debrid" else path
        rows.append([t.capitalize(), r["canonical_manifest"], debrid, download, path_str])
    _table(["Type", "Manifest", "Debrid instance", "Download instance", "Default"], rows)
    print()
    return 0


def cmd_wizard(api: APIClient, _args: argparse.Namespace) -> int:
    print()
    print(bold("  SLOP Setup Wizard"))
    print(dim("  Press Enter to accept defaults\n"))

    def ask(prompt: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            val = input(f"  {prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        return val or default

    domain = ask("Base domain (e.g. example.com)")
    if not domain or "." not in domain:
        _err("A valid domain is required.")
        return 1

    config_root = ask("Config root", "/var/lib/slop/config")
    media_root = ask("Media root", "/mnt/media")
    acme_email = ask("ACME email", f"admin@{domain}")
    dns_provider = ask("DNS provider for cert (cloudflare/route53/namecheap/…)", "cloudflare")
    tz = ask("Timezone", "America/Los_Angeles")

    payload = {
        "domain": domain,
        "config_root": config_root,
        "media_root": media_root,
        "acme_email": acme_email,
        "dns_provider": dns_provider,
        "timezone": tz,
        "puid": 1000,
        "pgid": 1000,
        "include_zerossl": True,
    }

    print()
    print("Running wizard…")
    result = api.post("/platform/wizard/run", payload)

    for step in result.get("steps", []):
        dot = _status_dot(step["status"])
        print(f"  {dot} {step['message']}")
        if step.get("detail") and step["status"] == "error":
            print(f"    {dim(step['detail'])}")

    if result.get("platform_ready"):
        print()
        _ok("Platform configured! Install apps with: ms apps install <key>")
        return 0
    else:
        _err(result.get("error") or "Setup did not complete.")
        return 1


# ── Argument parser ────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ms",
        description="SLOP CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              ms status
              ms apps install sonarr
              ms apps list
              ms catalog arr
              ms health
              ms routing
              ms --url http://192.168.1.100:8080 status
        """),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MS_URL", "http://localhost:8080"),
        metavar="URL",
        help="SLOP API base URL (default: $MS_URL or http://localhost:8080)",
    )

    sub = parser.add_subparsers(dest="command", title="commands")

    # status
    sub.add_parser("status", help="Platform and stack overview")

    # apps
    apps_p = sub.add_parser("apps", help="Manage installed apps")
    apps_sub = apps_p.add_subparsers(dest="apps_command", title="apps commands")

    apps_sub.add_parser("list", help="List installed apps")

    p = apps_sub.add_parser("install", help="Install an app")
    p.add_argument("key", help="App key (e.g. sonarr)")

    p = apps_sub.add_parser("remove", help="Remove an app")
    p.add_argument("key", help="App key")
    p.add_argument("--delete-config", action="store_true",
                   help="Also delete config folder")

    p = apps_sub.add_parser("disable", help="Disable an app")
    p.add_argument("key", help="App key")

    p = apps_sub.add_parser("enable", help="Re-enable a disabled app")
    p.add_argument("key", help="App key")

    p = apps_sub.add_parser("restart", help="Restart a running app")
    p.add_argument("key", help="App key")

    p = apps_sub.add_parser("logs", help="Show container logs")
    p.add_argument("key", help="App key")
    p.add_argument("--tail", type=int, default=100, help="Number of lines (default 100)")

    # catalog
    p = sub.add_parser("catalog", help="Browse available apps")
    p.add_argument("search", nargs="?", default="", help="Filter by name, tag, or description")

    # health
    health_p = sub.add_parser("health", help="Health checks")
    health_sub = health_p.add_subparsers(dest="health_command")
    health_sub.add_parser("status", help="Show last health cycle results")

    # infra
    sub.add_parser("infra", help="Infrastructure slot status")

    # routing
    sub.add_parser("routing", help="Media type routing config")

    # wizard
    sub.add_parser("wizard", help="Interactive platform setup wizard")

    return parser


def _dispatch_apps(api: APIClient, args: Any, parser: Any) -> int:
    """Dispatch apps subcommands."""
    _apps_dispatch = {
        "list": cmd_apps_list,
        "install": cmd_apps_install,
        "remove": cmd_apps_remove,
        "disable": cmd_apps_disable,
        "enable": cmd_apps_enable,
        "restart": cmd_apps_restart,
        "logs": cmd_apps_logs,
    }
    fn = _apps_dispatch.get(args.apps_command)
    if fn is not None:
        return fn(api, args)
    parser.parse_args(["apps", "--help"])
    return 0


def _dispatch(api: APIClient, args: Any, parser: Any) -> int:
    """Route top-level commands to handlers."""
    if args.command == "status":
        return cmd_status(api, args)
    if args.command == "apps":
        return _dispatch_apps(api, args, parser)
    if args.command == "catalog":
        return cmd_catalog(api, args)
    if args.command == "health":
        if getattr(args, "health_command", None) == "status":
            return cmd_health_status(api, args)
        return cmd_health(api, args)
    if args.command == "infra":
        return cmd_infra(api, args)
    if args.command == "routing":
        return cmd_routing(api, args)
    if args.command == "wizard":
        return cmd_wizard(api, args)
    parser.print_help()
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    api = APIClient(args.url)

    try:
        return _dispatch(api, args, parser)
    except APIError as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
