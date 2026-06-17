"""installer/tests/test_fetch.py — unit tests for installer/fetch.py.

All I/O is mocked via fetch_repo keyword-only injection.  No real git
calls, no real network access, no real filesystem clones.

Coverage:
  TestParseV5Semver        — semver tuple parsing and sort key behaviour
  TestResolveVersionRef    — explicit version bypass; remote tag resolution
  TestFetchRepoIdempotency — state-file short-circuit (no-op) path
  TestFetchRepoClone       — clone + .git removal ordering; error propagation
  TestFetchRepoReturnValue — returned resolved-tag string
"""

from __future__ import annotations

from pathlib import Path

import pytest

from installer._run import MissingBinaryError
from installer.fetch import (
    CloneNetworkError,
    FetchError,
    VersionTagNotFoundError,
    _V5_TAG_RE,
    _list_remote_tags,
    _parse_v5_semver,
    _resolve_version_ref,
    _run_git_clone,
    fetch_repo,
)


# ── Shared helpers ────────────────────────────────────────────────────────────


class _FakeState:
    def __init__(self, version: str):
        self.slop_version = version


def _noop_clone(url: str, dest: Path, ref: str) -> None:
    pass


def _noop_remove(dest: Path) -> None:
    pass


def _passing_kwargs(
    tags: list | None = None,
    state=None,
) -> dict:
    """Return fetch_repo injectable kwargs for a straightforward fetch."""
    if tags is None:
        tags = ["v5.1.0"]
    return {
        "list_remote_tags": lambda url: tags,
        "run_git_clone": _noop_clone,
        "remove_git_dir": _noop_remove,
        "read_state": lambda path: state,
    }


# ── TestParseV5Semver ─────────────────────────────────────────────────────────


class TestParseV5Semver:
    def test_standard_v5_tag(self):
        assert _parse_v5_semver("v5.1.0") == (5, 1, 0)

    def test_higher_patch(self):
        assert _parse_v5_semver("v5.2.10") == (5, 2, 10)

    def test_zero_everything(self):
        assert _parse_v5_semver("v5.0.0") == (5, 0, 0)

    def test_no_v_prefix(self):
        assert _parse_v5_semver("5.1.0") == (5, 1, 0)

    def test_unparseable_returns_empty_tuple(self):
        assert _parse_v5_semver("not-a-version") == ()

    def test_prerelease_suffix_returns_empty_tuple(self):
        # "0-alpha" cannot be cast to int → empty tuple
        assert _parse_v5_semver("v5.1.0-alpha") == ()

    def test_sort_descending_picks_highest(self):
        tags = ["v5.0.1", "v5.2.0", "v5.1.3"]
        tags.sort(key=_parse_v5_semver, reverse=True)
        assert tags[0] == "v5.2.0"

    def test_sort_minor_beats_patch(self):
        tags = ["v5.1.9", "v5.2.0"]
        tags.sort(key=_parse_v5_semver, reverse=True)
        assert tags[0] == "v5.2.0"


# ── TestResolveVersionRef ─────────────────────────────────────────────────────


class TestResolveVersionRef:
    def _list_tags(self, tags: list):
        return lambda url: tags

    def test_explicit_version_returned_as_is(self):
        result = _resolve_version_ref("v5.3.0", "unused", self._list_tags([]))
        assert result == "v5.3.0"

    def test_explicit_version_skips_remote_lookup(self):
        called = []
        _resolve_version_ref("v5.3.0", "url", lambda url: called.append(url) or [])
        assert called == []

    def test_none_returns_latest_v5_tag(self):
        tags = ["v5.0.1", "v5.1.0", "v4.9.9"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "v5.1.0"

    def test_none_picks_highest_semver_minor(self):
        tags = ["v5.2.0", "v5.10.0", "v5.9.9"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "v5.10.0"

    def test_none_ignores_non_v5_tags(self):
        tags = ["v4.9.9", "v6.0.0", "v5.1.0"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "v5.1.0"

    def test_no_v5_tags_raises(self):
        with pytest.raises(VersionTagNotFoundError):
            _resolve_version_ref(None, "url", self._list_tags(["v4.9.9"]))

    def test_empty_tag_list_raises(self):
        with pytest.raises(VersionTagNotFoundError):
            _resolve_version_ref(None, "url", self._list_tags([]))

    def test_network_error_propagates(self):
        def fail_list(url):
            raise CloneNetworkError("unreachable")

        with pytest.raises(CloneNetworkError):
            _resolve_version_ref(None, "url", fail_list)

    def test_version_not_found_message_names_repo(self):
        with pytest.raises(VersionTagNotFoundError, match="my-repo"):
            _resolve_version_ref(None, "my-repo", self._list_tags([]))

    def test_resolve_version_ref_filters_pre_release_tags(self):
        tags = ["v5.0.0-pre0", "v5.0.0-pre4", "v5.0.1"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "v5.0.1"

    def test_resolve_version_ref_returns_main_when_no_final_tags(self):
        tags = ["v5.0.0-pre0", "v5.0.0-pre1", "v5.0.0-pre2"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "main"

    def test_resolve_version_ref_prefers_final_over_pre_release_when_both_exist(self):
        tags = ["v5.0.0-pre4", "v5.0.0", "v5.0.0-pre0"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "v5.0.0"

    def test_resolve_version_ref_returns_highest_final_when_multiple_exist(self):
        tags = ["v5.0.0-pre4", "v5.0.0", "v5.1.0", "v5.0.1"]
        result = _resolve_version_ref(None, "url", self._list_tags(tags))
        assert result == "v5.1.0"


# ── TestFetchRepoIdempotency ──────────────────────────────────────────────────


class TestFetchRepoIdempotency:
    def test_matching_state_skips_clone(self):
        clones = []
        kwargs = _passing_kwargs(state=_FakeState("v5.1.0"))
        kwargs["run_git_clone"] = lambda url, dest, ref: clones.append(ref)
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert clones == []

    def test_matching_state_skips_remove_git_dir(self):
        removes = []
        kwargs = _passing_kwargs(state=_FakeState("v5.1.0"))
        kwargs["remove_git_dir"] = lambda dest: removes.append(dest)
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert removes == []

    def test_matching_state_returns_resolved_tag(self):
        result = fetch_repo("/some/dir", "v5.1.0", **_passing_kwargs(state=_FakeState("v5.1.0")))
        assert result == "v5.1.0"

    def test_different_state_version_proceeds(self):
        clones = []
        kwargs = _passing_kwargs(state=_FakeState("v5.0.0"))
        kwargs["run_git_clone"] = lambda url, dest, ref: clones.append(ref)
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert clones == ["v5.1.0"]

    def test_no_state_file_proceeds(self):
        clones = []
        kwargs = _passing_kwargs(state=None)
        kwargs["run_git_clone"] = lambda url, dest, ref: clones.append(ref)
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert clones == ["v5.1.0"]


# ── TestFetchRepoClone ────────────────────────────────────────────────────────


class TestFetchRepoClone:
    def test_remove_git_dir_called_after_clone(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["run_git_clone"] = lambda url, dest, ref: call_order.append("clone")
        kwargs["remove_git_dir"] = lambda dest: call_order.append("remove")
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert call_order == ["clone", "remove"]

    def test_clone_receives_correct_repo_url(self):
        urls_seen = []
        kwargs = _passing_kwargs()
        kwargs["run_git_clone"] = lambda url, dest, ref: urls_seen.append(url)
        fetch_repo("/some/dir", "v5.1.0", repo_url="https://example.com/repo.git", **kwargs)
        assert urls_seen == ["https://example.com/repo.git"]

    def test_clone_receives_resolved_ref(self):
        refs_seen = []
        kwargs = _passing_kwargs()
        kwargs["run_git_clone"] = lambda url, dest, ref: refs_seen.append(ref)
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert refs_seen == ["v5.1.0"]

    def test_clone_receives_dest_as_path(self):
        dests_seen = []
        kwargs = _passing_kwargs()
        kwargs["run_git_clone"] = lambda url, dest, ref: dests_seen.append(dest)
        fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert dests_seen == [Path("/some/dir")]

    def test_network_error_during_clone_propagates(self):
        def fail_clone(url, dest, ref):
            raise CloneNetworkError("clone failed")

        kwargs = _passing_kwargs()
        kwargs["run_git_clone"] = fail_clone
        with pytest.raises(CloneNetworkError):
            fetch_repo("/some/dir", "v5.1.0", **kwargs)

    def test_network_error_skips_remove_git_dir(self):
        removes = []

        def fail_clone(url, dest, ref):
            raise CloneNetworkError("clone failed")

        kwargs = _passing_kwargs()
        kwargs["run_git_clone"] = fail_clone
        kwargs["remove_git_dir"] = lambda dest: removes.append(dest)
        with pytest.raises(CloneNetworkError):
            fetch_repo("/some/dir", "v5.1.0", **kwargs)
        assert removes == []

    def test_ls_remote_failure_propagates_before_clone(self):
        clones = []

        def fail_list(url):
            raise CloneNetworkError("network down")

        kwargs = _passing_kwargs()
        kwargs["list_remote_tags"] = fail_list
        kwargs["run_git_clone"] = lambda url, dest, ref: clones.append(ref)
        with pytest.raises(CloneNetworkError):
            fetch_repo("/some/dir", **kwargs)
        assert clones == []


# ── TestFetchRepoReturnValue ──────────────────────────────────────────────────


class TestFetchRepoReturnValue:
    def test_returns_explicit_version(self):
        result = fetch_repo("/some/dir", "v5.3.0", **_passing_kwargs())
        assert result == "v5.3.0"

    def test_returns_resolved_version_when_none_given(self):
        kwargs = _passing_kwargs(tags=["v5.0.1", "v5.2.0", "v5.1.0"])
        result = fetch_repo("/some/dir", **kwargs)
        assert result == "v5.2.0"

    def test_idempotent_return_equals_resolved(self):
        result = fetch_repo("/some/dir", "v5.1.0", **_passing_kwargs(state=_FakeState("v5.1.0")))
        assert result == "v5.1.0"


# ── TestV5TagRegex ────────────────────────────────────────────────────────────


class TestV5TagRegex:
    """_V5_TAG_RE matches v5 semver with optional pre-release suffix (Step 2.8)."""

    def test_prerelease_pre0_matches(self):
        assert _V5_TAG_RE.match("v5.0.0-pre0") is not None

    def test_prerelease_rc1_matches(self):
        assert _V5_TAG_RE.match("v5.0.0-rc1") is not None

    def test_prerelease_alpha_dot_1_matches(self):
        assert _V5_TAG_RE.match("v5.1.0-alpha.1") is not None

    def test_v4_does_not_match(self):
        assert _V5_TAG_RE.match("v4.2.0") is None

    def test_v6_does_not_match(self):
        assert _V5_TAG_RE.match("v6.0.0") is None

    def test_plain_v5_still_matches(self):
        assert _V5_TAG_RE.match("v5.1.0") is not None

    def test_resolve_with_only_prerelease_tag_returns_main(self):
        # Pre-release tags are internal milestones, not operator install targets
        # (docs/RELEASE_PROCESS.md). Removed assertion of the old buggy behavior.
        result = _resolve_version_ref(None, "url", lambda url: ["v5.0.0-pre0"])
        assert result == "main"


# ── TestRunGitClonePreservesExistingDir ───────────────────────────────────────


class TestRunGitClonePreservesExistingDir:
    """_run_git_clone succeeds when install_dir already has content (Step 2.8 Bug #4).

    ADR 0013 §2 pre-write creates install_dir before fetch_repo is called.
    git refuses to clone into a non-empty directory, so _run_git_clone clones
    to a sibling temp dir then moves all entries in, preserving existing files.
    """

    def test_existing_state_file_preserved(self, tmp_path):
        from unittest.mock import patch
        from installer.fetch import _run_git_clone

        install_dir = tmp_path / "install"
        install_dir.mkdir()
        state_file = install_dir / ".installer-state.json"
        state_file.write_text('{"phase": "installing"}')

        tmp_clone = install_dir.parent / (install_dir.name + ".clone-tmp")

        def fake_clone(*args, **kwargs):
            import types

            tmp_clone.mkdir(parents=True, exist_ok=True)
            (tmp_clone / "app.py").write_text("# repo content")
            (tmp_clone / ".git").mkdir()
            return types.SimpleNamespace(returncode=0, stderr="")

        with patch("installer._run.subprocess.run", side_effect=fake_clone):
            _run_git_clone("https://example.com/repo.git", install_dir, "v5.0.0-pre0")

        assert state_file.exists(), "pre-write state file must be preserved"
        assert state_file.read_text() == '{"phase": "installing"}'
        assert (install_dir / "app.py").exists(), "cloned content must be moved in"
        assert not tmp_clone.exists(), "temp clone dir must be cleaned up"


# ── TestFetchBoundaryProbe ────────────────────────────────────────────────────


class TestFetchBoundaryProbe:
    """F9/F12 boundary tests — git absent surfaces MissingBinaryError; missing parent
    directory surfaces FetchError before the git invocation.
    """

    def test_list_remote_tags_raises_on_git_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(MissingBinaryError, match="git"):
                _list_remote_tags("https://example.com/repo.git")

    def test_run_git_clone_raises_on_git_absent(self, tmp_path):
        from unittest.mock import patch

        dest = tmp_path / "install"
        dest.mkdir()
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(MissingBinaryError, match="git"):
                _run_git_clone("https://example.com/repo.git", dest, "v5.1.0")

    def test_run_git_clone_raises_on_missing_parent(self, tmp_path):
        dest = tmp_path / "nonexistent" / "install"
        with pytest.raises(FetchError, match="nonexistent"):
            _run_git_clone("https://example.com/repo.git", dest, "v5.1.0")
