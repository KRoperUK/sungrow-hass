#!/usr/bin/env bash
# Verify a GitHub release tag points at the expected commit SHA.
# Env:
#   TAG          release tag name
#   COMMIT_SHA   expected commit
#   GH_TOKEN / GITHUB_REPOSITORY set by Actions
set -euo pipefail

if [ -z "${TAG:-}" ] || [ -z "${COMMIT_SHA:-}" ]; then
  echo "TAG and COMMIT_SHA are required" >&2
  exit 2
fi

tag_sha=$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${TAG}" --jq '.object.sha')
tag_type=$(gh api "repos/${GITHUB_REPOSITORY}/git/ref/tags/${TAG}" --jq '.object.type')
if [ "$tag_type" = "tag" ]; then
  tag_sha=$(gh api "repos/${GITHUB_REPOSITORY}/git/tags/${tag_sha}" --jq '.object.sha')
fi

if [ "$tag_sha" != "$COMMIT_SHA" ]; then
  echo "FAIL: tag ${TAG} -> ${tag_sha}, expected ${COMMIT_SHA}" >&2
  exit 1
fi
echo "OK: tag ${TAG} -> ${tag_sha}"
