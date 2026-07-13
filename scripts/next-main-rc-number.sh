#!/usr/bin/env bash
# Print the next main-branch RC number for a given version.
#
# Usage: next-main-rc-number.sh <version>
#   version — no leading v (e.g. 3.5.0)
#
# Looks at BOTH:
#   1. GitHub release tag names (gh release list)
#   2. Git refs under tags/v<ver>-rc.* and legacy tags/dev-v<ver>-rc.*
#      (including orphaned tags left behind after an immutable release was deleted)
#
# so a deleted-but-immutable rc.N cannot be reused. Prints an integer N
# suitable for v<ver>-rc.N (highest known + 1, or 1 if none).
set -euo pipefail

VERSION="${1:?version required (e.g. 3.5.0)}"

if [ -z "${GITHUB_REPOSITORY:-}" ]; then
  echo "GITHUB_REPOSITORY is required" >&2
  exit 2
fi

version_re=$(printf '%s' "${VERSION}" | sed 's/\./\\./g')
# Current shape: vX.Y.Z-rc.N ; legacy: dev-vX.Y.Z-rc.N
pattern="^(dev-)?v${version_re}-rc\\.[0-9]+$"

collect_rc_numbers() {
  {
    # Live / listed releases.
    gh release list --limit 1000 --json tagName --jq '.[].tagName' 2>/dev/null || true
    # All matching tags on the default remote, including orphans after
    # immutable-release delete (release list no longer shows them).
    gh api "repos/${GITHUB_REPOSITORY}/git/matching-refs/tags/v${VERSION}-rc." \
      --paginate --jq '.[].ref' 2>/dev/null \
      | sed 's#^refs/tags/##' || true
    gh api "repos/${GITHUB_REPOSITORY}/git/matching-refs/tags/dev-v${VERSION}-rc." \
      --paginate --jq '.[].ref' 2>/dev/null \
      | sed 's#^refs/tags/##' || true
  } | grep -E "${pattern}" | sed -E 's/^.*-rc\.//' | grep -E '^[0-9]+$' || true
}

highest=$(collect_rc_numbers | sort -n | tail -1 || true)
printf '%s\n' "$((${highest:-0} + 1))"
