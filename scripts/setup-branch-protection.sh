#!/usr/bin/env bash
#
# Configure branch protection for the default branch.
#
# Requires the GitHub CLI (`gh`) authenticated as a repo admin.
# Usage: scripts/setup-branch-protection.sh [owner/repo] [branch]
#
set -euo pipefail

REPO="${1:-KRoperUK/sungrow-hass}"
BRANCH="${2:-main}"

echo "Applying branch protection to ${REPO}@${BRANCH}..."

gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  "repos/${REPO}/branches/${BRANCH}/protection" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["lint", "test", "hacs_validate"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON

echo "Done. Current protection summary:"
gh api "repos/${REPO}/branches/${BRANCH}/protection" \
  --jq '{required_checks: .required_status_checks.contexts, strict: .required_status_checks.strict, reviews: .required_pull_request_reviews.required_approving_review_count, linear: .required_linear_history.enabled, force_pushes: .allow_force_pushes.enabled, deletions: .allow_deletions.enabled}'
