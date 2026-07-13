#!/usr/bin/env bash
# Compute a semver pre-release version string and Git tag for a dev build.
#
# Env inputs:
#   VERSION          predicted next version, no leading v (e.g. 3.5.0)
#   HEAD_BRANCH      branch CI ran on (main, or a PR source branch)
#   WORKFLOW_EVENT   CI run's triggering event (push / pull_request)
#   RC_NUMBER        incrementing RC counter for the current version (main push)
#   PR_NUMBER        pull request number (PR builds)
#   SHORT_SHA        short commit SHA for manifest version (PR builds)
#   RUN_ID           globally-unique run id for tag uniqueness (PR builds)
#
# Prints two lines:
#   version=<bare pre-release for manifest/const>
#   tag=v<same or tag-unique form>
#
# Main:  version=X.Y.Z-rc.N     tag=vX.Y.Z-rc.N
# PR:    version=X.Y.Z-pr.P.S   tag=vX.Y.Z-pr.P.R  (S=short SHA, R=run id)
#
# RC uses a dotted numeric identifier (rc.N) so pre-releases order numerically
# (3.3.0-rc.10 > 3.3.0-rc.9), per semver.org §11.
#
# Does not write GITHUB_OUTPUT (callers map lines explicitly) so rollover loops
# can re-invoke safely.
set -euo pipefail

if [ -z "${VERSION:-}" ]; then
  echo "VERSION is required" >&2
  exit 2
fi

if [ "${HEAD_BRANCH:-}" = "main" ] && [ "${WORKFLOW_EVENT:-}" = "push" ]; then
  if [ -z "${RC_NUMBER:-}" ]; then
    echo "RC_NUMBER is required for main RC builds" >&2
    exit 2
  fi
  ver="${VERSION}-rc.${RC_NUMBER}"
  tag="v${ver}"
else
  if [ -z "${PR_NUMBER:-}" ] || [ -z "${SHORT_SHA:-}" ] || [ -z "${RUN_ID:-}" ]; then
    echo "PR_NUMBER, SHORT_SHA, and RUN_ID are required for PR builds" >&2
    exit 2
  fi
  # Manifest uses short SHA for readability; tag uses run id for uniqueness.
  short=$(printf '%s' "${SHORT_SHA}" | cut -c1-7)
  ver="${VERSION}-pr.${PR_NUMBER}.${short}"
  tag="v${VERSION}-pr.${PR_NUMBER}.${RUN_ID}"
fi

printf 'version=%s\n' "$ver"
printf 'tag=%s\n' "$tag"
