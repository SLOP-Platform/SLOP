"""backend/core/compose_cli.py — docker compose subprocess command helpers (#1302 split).

Extracted from ``compose.py`` (which grew past the 500-line ``production_code`` cap during the
CI-dead window, #1271) to separate the **shell-out command layer** from the fragment-building /
merging logic. These wrap ``docker compose pull/up/down`` for a single fragment, always passing the
shared ``--env-file`` so env vars resolve regardless of cwd; ``compose_pull_stream`` streams cleaned
progress lines for the install UI.

The helpers hold no module state and lazy-import ``config`` / ``subprocess`` inside each function, so
this module has **no runtime import of** ``compose`` → no import cycle. ``compose`` imports the four
public helpers back and re-exports them (redundant-alias idiom), so every existing caller — the infra
providers' ``from backend.core.compose import compose_up`` etc. — keeps resolving unchanged. Pure
move, no behaviour change.
"""

from __future__ import annotations

import asyncio
import os
import re as _re
from collections.abc import AsyncIterator
from pathlib import Path

_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[mGKHF]")
_SPINNER_CHARS = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")


def _clean_compose_line(line: str) -> str:
    """Strip ANSI codes and spinner chars from a single compose output line."""
    line = _ANSI_RE.sub("", line).strip()
    if not line or all(c in _SPINNER_CHARS for c in line):
        return ""
    return line


async def compose_pull_stream(
    frag_path: Path,
    timeout: int = 600,
) -> AsyncIterator[str]:
    """Async generator: yields cleaned progress lines from docker compose pull."""
    from backend.core.config import config as _cfg

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(frag_path),
        "--env-file",
        str(_cfg.env_file),
        "pull",
        "--progress=plain",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ},
    )
    try:
        async for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.decode("utf-8", errors="replace").rstrip()
            clean = _clean_compose_line(line)
            if clean:
                yield clean
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        yield f"ERROR: pull timed out after {timeout}s"


def compose_pull(frag_path: Path, timeout: int = 600) -> tuple[int, str]:
    """Pull images declared in a fragment. Separate from compose_up so
    large-image pulls get a long timeout without affecting container-start."""
    import subprocess as _sp
    from backend.core.config import config as _cfg

    cmd = [
        "docker",
        "compose",
        "-f",
        str(frag_path),
        "--env-file",
        str(_cfg.env_file),
        "pull",
    ]
    r = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stderr or r.stdout).strip()


def compose_up(frag_path: Path, pull: bool = False, timeout: int = 120) -> tuple[int, str]:
    """Run `docker compose up -d` for a fragment, always passing the shared .env file.

    Returns (returncode, error_output).
    The --env-file flag ensures env vars (CF_DNS_API_TOKEN, etc.) resolve
    correctly regardless of the working directory compose is called from.
    """
    import subprocess as _sp
    from backend.core.config import config as _cfg

    cmd = [
        "docker",
        "compose",
        "-f",
        str(frag_path),
        "--env-file",
        str(_cfg.env_file),
        "up",
        "-d",
    ]
    if pull:
        cmd += ["--pull", "missing"]
    r = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stderr or r.stdout).strip()


def compose_down(frag_path: Path, timeout: int = 60) -> tuple[int, str]:
    """Run `docker compose down` for a fragment, always passing the shared .env file."""
    import subprocess as _sp
    from backend.core.config import config as _cfg

    cmd = [
        "docker",
        "compose",
        "-f",
        str(frag_path),
        "--env-file",
        str(_cfg.env_file),
        "down",
        # NO --remove-orphans here. Every app fragment lives in the same
        # directory (data/compose/), so they all share compose's default
        # project name (the dir basename). A single fragment only declares its
        # own service, so `down --remove-orphans` scoped to that shared project
        # treats EVERY other managed container — including the Traefik reverse
        # proxy — as an orphan and deletes it. Removing one app must not tear
        # down the rest of the stack. --remove-orphans is only safe on a compose
        # file that represents the whole project.
    ]
    r = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stderr or r.stdout).strip()
