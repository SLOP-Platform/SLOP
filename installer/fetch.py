"""installer/fetch.py — repo clone and version-pin step for the v5 installer.

fetch_repo() clones the slop repo to install_dir at a pinned tag and
removes .git/ so the installed copy is not a git working tree.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from collections.abc import Callable

from installer._run import run_required
from installer.state import STATE_FILE_NAME, read_state_file

_REPO_URL: str = "https://github.com/Nnyan/SLOP.git"
_V5_TAG_RE: re.Pattern = re.compile(r"^v5\.\d+\.\d+(-[\w.]+)?$")


# ── Error classes ─────────────────────────────────────────────────────────────


class FetchError(Exception):
    pass


class CloneNetworkError(FetchError):
    pass


class VersionTagNotFoundError(FetchError):
    pass


# ── I/O helpers (replaceable in tests via fetch_repo kwargs) ──────────────────


def _list_remote_tags(repo_url: str) -> list:
    """Return tag names from the remote repo (no peeled ^{} refs)."""
    result = run_required(["git", "ls-remote", "--tags", repo_url])
    if result.returncode != 0:
        raise CloneNetworkError(
            f"Could not reach repository at {repo_url}. Check network connectivity and re-run."
        )
    tags = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags.append(ref[len("refs/tags/") :])
    return tags


def _run_git_clone(url: str, dest: Path, ref: str) -> None:
    # Clone to a sibling temp dir so git never sees the pre-write state file
    # that write_state_file already placed in dest (ADR 0013 §2).  git refuses
    # to clone into a non-empty directory — cloning to a sibling then moving
    # preserves any existing dest content (e.g. the state file).
    if not dest.parent.exists():
        raise FetchError(
            f"Clone destination parent directory does not exist: {dest.parent!s}. "
            "Create the parent directory before running the installer."
        )
    tmp = dest.parent / (dest.name + ".clone-tmp")
    try:
        result = run_required(["git", "clone", "--branch", ref, "--depth", "1", url, str(tmp)])
        if result.returncode != 0:
            raise CloneNetworkError(
                f"git clone failed for {url} at ref {ref!r}: {result.stderr.strip()}"
            )
        dest.mkdir(parents=True, exist_ok=True)
        for item in tmp.iterdir():
            shutil.move(str(item), str(dest / item.name))
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def _remove_git_dir(dest: Path) -> None:
    git_dir = dest / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)


# ── Version resolution ────────────────────────────────────────────────────────


def _parse_v5_semver(tag: str) -> tuple:
    """Return (major, minor, patch) tuple for sorting; empty tuple if unparseable."""
    stripped = tag.lstrip("v")
    parts = []
    for segment in stripped.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            return ()
    return tuple(parts)


def _resolve_version_ref(
    version_ref: str | None,
    repo_url: str,
    list_remote_tags: Callable[[str], list],
) -> str:
    """Return the concrete tag to clone.

    Explicit version_ref is returned as-is.  None triggers a remote tag
    lookup to find the highest v5.x.y tag.
    """
    if version_ref is not None:
        # expected format: v1.2.3 (semver with leading v)
        if not re.match(r"^v\d+\.\d+\.\d+$", version_ref):
            raise ValueError(
                f"Invalid --version-ref {version_ref!r}: expected format is v1.2.3 "
                "(semver with leading 'v', e.g. v5.1.0)."
            )
        return version_ref

    tags = list_remote_tags(repo_url)
    v5_tags = [t for t in tags if _V5_TAG_RE.match(t)]

    if not v5_tags:
        raise VersionTagNotFoundError(
            f"No v5.x.y tags found in repository {repo_url}. "
            "Specify a version_ref explicitly or push a v5.x.y tag first."
        )

    # Filter to final-release tags only; pre-release tags (e.g. v5.0.0-pre0)
    # return () from _parse_v5_semver and are NOT operator install targets per
    # docs/RELEASE_PROCESS.md.
    final_v5_tags = [t for t in v5_tags if _parse_v5_semver(t) != ()]

    if not final_v5_tags:
        # No final-release v5 tag exists yet; fall back to main
        return "main"

    final_v5_tags.sort(key=_parse_v5_semver, reverse=True)
    return final_v5_tags[0]


# ── Public entry point ────────────────────────────────────────────────────────


def fetch_repo(
    install_dir,
    version_ref: str | None = None,
    *,
    repo_url: str = _REPO_URL,
    list_remote_tags: Callable[[str], list] = _list_remote_tags,
    run_git_clone: Callable[[str, Path, str], None] = _run_git_clone,
    remove_git_dir: Callable[[Path], None] = _remove_git_dir,
    read_state: Callable = read_state_file,
) -> str:
    """Clone the slop repo to install_dir at the given version_ref.

    Returns the resolved version tag (the tag that was or would have been
    cloned).  The caller should store this in the state file.

    Idempotency: if install_dir already contains a state file whose
    slop_version matches the resolved tag, the clone is skipped and
    the resolved tag is returned immediately.

    The .git/ directory is removed after a successful clone — installed
    copies are not git working trees (ADR 0013 §1).

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers pass only install_dir and optionally version_ref.
    """
    dest = Path(install_dir)

    resolved = _resolve_version_ref(version_ref, repo_url, list_remote_tags)

    # Idempotency: state file present and version matches → no-op
    state = read_state(dest / STATE_FILE_NAME)
    if state is not None and state.slop_version == resolved:
        return resolved

    run_git_clone(repo_url, dest, resolved)
    remove_git_dir(dest)

    return resolved
