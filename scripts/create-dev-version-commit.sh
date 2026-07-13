#!/usr/bin/env bash
# Create a synthetic commit that only rewrites integration version files.
#
# Usage: create-dev-version-commit.sh <version>
#   Parent is the current HEAD (tested SHA). Does not push.
#
# Prints the new commit SHA on stdout.
set -euo pipefail

VERSION="${1:?version required}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bash scripts/write-dev-version.sh "$VERSION"

git add custom_components/sungrow/manifest.json custom_components/sungrow/const.py

if git diff --cached --quiet; then
  echo "error: no version file changes staged (already at ${VERSION}?)" >&2
  exit 1
fi

# Bot-only synthetic commit (version files). Use one-shot -c identity so we never
# rewrite the caller's local user.name/email.
# --quiet keeps stdout clean: only the SHA below is emitted for capture.
git -c user.name="github-actions[bot]" \
  -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet --no-verify -m "chore(dev): set version ${VERSION}"
git rev-parse HEAD
