#!/usr/bin/env bash
# Compute a dev pre-release tag from the CI workflow_run context.
# Env inputs:
#   VERSION        predicted next version, no leading v (e.g. 3.1.0)
#   HEAD_BRANCH    branch CI ran on (main, or a PR source branch)
#   WORKFLOW_EVENT CI run's triggering event (push / pull_request)
#   RC_NUMBER      incrementing RC counter for the current version (main push)
#   RUN_ID         globally-unique run id (PR branch builds)
# Prints the tag to stdout: dev-v<ver>-rc.<n> (main push) or dev-v<ver>-<branch>-<id>.
# The RC uses a *dotted* numeric identifier (rc.N) so the embedded version stays a valid
# semver pre-release that orders numerically (3.3.0-rc.10 > 3.3.0-rc.9), per semver.org §11.
set -euo pipefail

sanitize() { printf '%s' "$1" | sed 's/[^a-zA-Z0-9._-]/-/g' | sed 's/-\{2,\}/-/g'; }

if [ "${HEAD_BRANCH}" = "main" ] && [ "${WORKFLOW_EVENT}" = "push" ]; then
    printf 'dev-v%s-rc.%s\n' "${VERSION}" "${RC_NUMBER}"
else
    printf 'dev-v%s-%s-%s\n' "${VERSION}" "$(sanitize "${HEAD_BRANCH}")" "${RUN_ID}"
fi
