#!/usr/bin/env bash
# installer/generate_tree_checksums.sh — Generate a per-release tree.checksums
# manifest for integrity verification by the v5 installer.
#
# Usage (run from the repo root of a clean checkout at the release tag):
#   bash installer/generate_tree_checksums.sh
#
# The manifest is written to tree.checksums in the current directory.
# Upload it as a release asset alongside the published tag.
#
# Format: standard sha256sum output (one <hash>  ./path per line), covering
# all git-tracked files.  .git/ internals are excluded.

set -euo pipefail

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: not inside a git repository." >&2
  exit 1
fi

_HEAD_REF="$(git rev-parse --abbrev-ref HEAD)"

echo "Generating tree.checksums from ref: ${_HEAD_REF}"
echo "  (git-tracked files only; .git/ internals excluded)"
echo ""

git ls-files -z | xargs -0 sha256sum | sort -k2 > tree.checksums

_FILE_COUNT="$(wc -l < tree.checksums)"
echo "Done: ${_FILE_COUNT} files checksummed → tree.checksums"
