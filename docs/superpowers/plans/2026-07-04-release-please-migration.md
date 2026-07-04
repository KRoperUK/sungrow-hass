# Release-Please Migration + Three-Tier Dev Builds — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bespoke release workflows with release-please, and add CI-gated dev
pre-releases (`main`→rc, PR branch→branch build) versioned to the next release-please version.

**Architecture:** See [the design spec](../specs/2026-07-04-release-please-migration-design.md).
release-please owns real releases on `main`; `dev-build.yml` (on CI success) publishes dev
pre-releases; cleanup is event-driven + a weekly sweep.

**Tech Stack:** GitHub Actions, `googleapis/release-please-action@v4`,
`TriPSs/conventional-changelog-action@v6`, `gh` CLI, bash.

## Global Constraints

- **Pin every action by commit SHA** with a `# vX` comment (repo convention). Reuse SHAs already
  in the repo: checkout `9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0`;
  conventional-changelog-action `952b14bbc4be87e8458a6ac5926fc655608b1b19 # v6`.
- **Never inline `${{ }}` into `run:` scripts** — pass via `env:`, reference as shell vars.
- Dev builds are GitHub **pre-releases**. Tags keep the `v`: `dev-vX.Y.Z-...`.
- `<run_id>` = `github.run_id`. Branch sanitisation: `sed 's/[^a-zA-Z0-9._-]/-/g; s/-\{2,\}/-/g'`.
- Work on branch `ci/release-please-migration` (already created; the design spec is committed there).

## File Structure

- Create: `release-please-config.json`, `.release-please-manifest.json` (repo root)
- Create: `.github/workflows/release-please.yml`, `.github/workflows/dev-build.yml`
- Create: `scripts/compute-dev-tag.sh`, `scripts/compute-dev-tag.test.sh`
- Modify: `.github/workflows/cleanup-pr-release.yml`
- Delete: `.github/workflows/release-pr.yml`, `.github/workflows/publish-release.yml`, `.github/workflows/release.yml`
- Runtime (no file change): close PRs #130/#134, delete their `release/v*` branches

---

### Task 1: release-please config + dry-run validation

**Files:**
- Create: `.release-please-manifest.json`
- Create: `release-please-config.json`

- [ ] **Step 1: Create `.release-please-manifest.json`** (seed to the last released version)

```json
{
  ".": "3.0.0"
}
```

- [ ] **Step 2: Create `release-please-config.json`**

```json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "packages": {
    ".": {
      "release-type": "simple",
      "extra-files": [
        { "type": "json", "path": "custom_components/sungrow/manifest.json", "jsonpath": "$.version" },
        { "type": "toml", "path": "pyproject.toml", "jsonpath": "$.project.version" }
      ]
    }
  }
}
```

- [ ] **Step 3: Dry-run to validate version + updated files**

Run (needs network + `npx`):
```bash
npx --yes release-please@16 release-pr \
  --token="$(gh auth token)" \
  --repo-url=KRoperUK/sungrow-hass \
  --config-file=release-please-config.json \
  --manifest-file=.release-please-manifest.json \
  --dry-run 2>&1 | tee /tmp/rp-dryrun.txt
```
Expected: logs a release PR for **v3.1.0** (the pending `feat` commits since v3.0.0) and lists
updates to `manifest.json` + `pyproject.toml` + `CHANGELOG.md`. **If it errors demanding a
`version.txt`:** replace the `simple` config's implicit updater by adding
`{ "type": "generic", "path": "custom_components/sungrow/manifest.json" }` with a
`# x-release-please-version` annotation approach, OR create a `version.txt` containing `3.0.0`
and add it to `extra-files`. Pick whichever the dry-run accepts; re-run until clean.

- [ ] **Step 4: Commit**

```bash
git add release-please-config.json .release-please-manifest.json
git commit -m "ci: add release-please config (simple, manifest + pyproject updaters)"
```

---

### Task 2: release-please workflow

**Files:**
- Create: `.github/workflows/release-please.yml`

**Interfaces:**
- Produces: on merge of the release PR, a `vX.Y.Z` tag + GitHub release, and `release_created`
  output that gates the dev-prune step.

- [ ] **Step 1: Resolve the release-please-action v4 SHA to pin**

```bash
gh api repos/googleapis/release-please-action/git/ref/tags/v4 \
  --jq '.object.sha // .object.url' 
# If it returns a tag object URL, dereference it:
gh api repos/googleapis/release-please-action/commits/v4 --jq .sha
```
Record the 40-char commit SHA as `<RP_SHA>` for Step 2.

- [ ] **Step 2: Create `.github/workflows/release-please.yml`** (substitute `<RP_SHA>`)

```yaml
name: Release Please

on:
    push:
        branches: [main]

permissions:
    contents: write
    pull-requests: write

concurrency:
    group: release-please
    cancel-in-progress: false

jobs:
    release-please:
        runs-on: ubuntu-latest
        timeout-minutes: 10
        steps:
            - name: Run release-please
              id: release
              uses: googleapis/release-please-action@<RP_SHA> # v4
              with:
                  token: ${{ github.token }}
                  config-file: release-please-config.json
                  manifest-file: .release-please-manifest.json

            # A published release supersedes every dev build. Prune them all so only
            # the real release remains (rc builds + any leftover branch builds).
            - name: Prune all dev pre-releases on a real release
              if: ${{ steps.release.outputs.release_created == 'true' }}
              env:
                  GH_TOKEN: ${{ github.token }}
                  GH_REPO: ${{ github.repository }}
              run: |
                  echo "Release created; deleting all dev-* pre-releases."
                  gh release list --limit 1000 --json tagName,isPrerelease \
                    --jq '.[] | select(.isPrerelease and (.tagName | startswith("dev-"))) | .tagName' \
                  | while read -r tag; do
                      echo "Deleting ${tag}"
                      gh release delete "$tag" --yes --cleanup-tag
                    done
```

- [ ] **Step 3: YAML parses**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/release-please.yml')); print('ok')"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/release-please.yml
git commit -m "ci: add release-please workflow (rolling release PR + dev-prune on release)"
```

---

### Task 3: dev-tag computation script (TDD)

**Files:**
- Create: `scripts/compute-dev-tag.sh`
- Create: `scripts/compute-dev-tag.test.sh`

**Interfaces:**
- Produces: `scripts/compute-dev-tag.sh` reads env `VERSION`, `HEAD_BRANCH`, `WORKFLOW_EVENT`,
  `RUN_ID` and prints the tag to stdout. Consumed by `dev-build.yml` (Task 4).

- [ ] **Step 1: Write the failing test** `scripts/compute-dev-tag.test.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sut="$here/compute-dev-tag.sh"
fail=0
check() { # desc expected VERSION HEAD_BRANCH WORKFLOW_EVENT RUN_ID
  local desc="$1" expected="$2" got
  got=$(VERSION="$3" HEAD_BRANCH="$4" WORKFLOW_EVENT="$5" RUN_ID="$6" bash "$sut")
  if [ "$got" = "$expected" ]; then echo "ok   - $desc"
  else echo "FAIL - $desc: expected '$expected' got '$got'"; fail=1; fi
}
check "main push -> rc"                 "dev-v3.1.0-rc123"        "3.1.0" "main"    "push"         "123"
check "PR branch -> branch build"       "dev-v3.1.0-feat-x-123"  "3.1.0" "feat-x"  "pull_request" "123"
check "slash in branch sanitised"       "dev-v3.1.0-feat-a-b-9"  "3.1.0" "feat/a/b" "pull_request" "9"
check "double dash collapsed"           "dev-v3.1.0-a-b-9"       "3.1.0" "a--b"    "pull_request" "9"
check "main via pull_request is branch" "dev-v3.1.0-main-9"      "3.1.0" "main"    "pull_request" "9"
[ "$fail" -eq 0 ] && echo "ALL PASS" || { echo "FAILURES"; exit 1; }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash scripts/compute-dev-tag.test.sh`
Expected: fails (script does not exist yet).

- [ ] **Step 3: Write `scripts/compute-dev-tag.sh`**

```bash
#!/usr/bin/env bash
# Compute a dev pre-release tag from the CI workflow_run context.
# Env inputs:
#   VERSION        predicted next version, no leading v (e.g. 3.1.0)
#   HEAD_BRANCH    branch CI ran on (main, or a PR source branch)
#   WORKFLOW_EVENT CI run's triggering event (push / pull_request)
#   RUN_ID         globally-unique run id
# Prints the tag to stdout: dev-v<ver>-rc<id> (main push) or dev-v<ver>-<branch>-<id>.
set -euo pipefail

sanitize() { printf '%s' "$1" | sed 's/[^a-zA-Z0-9._-]/-/g' | sed 's/-\{2,\}/-/g'; }

if [ "${HEAD_BRANCH}" = "main" ] && [ "${WORKFLOW_EVENT}" = "push" ]; then
    printf 'dev-v%s-rc%s\n' "${VERSION}" "${RUN_ID}"
else
    printf 'dev-v%s-%s-%s\n' "${VERSION}" "$(sanitize "${HEAD_BRANCH}")" "${RUN_ID}"
fi
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `chmod +x scripts/compute-dev-tag.sh scripts/compute-dev-tag.test.sh && bash scripts/compute-dev-tag.test.sh`
Expected: `ALL PASS`

- [ ] **Step 5: Commit**

```bash
git add scripts/compute-dev-tag.sh scripts/compute-dev-tag.test.sh
git commit -m "ci: add tested dev-tag computation script"
```

---

### Task 4: dev-build workflow

**Files:**
- Create: `.github/workflows/dev-build.yml`

**Interfaces:**
- Consumes: `scripts/compute-dev-tag.sh`; `conventional-changelog-action` outputs `version`/`skipped`.

- [ ] **Step 1: Create `.github/workflows/dev-build.yml`**

```yaml
name: Dev Build

on:
    workflow_run:
        workflows: [CI]
        types: [completed]
    workflow_dispatch: {}

permissions:
    contents: write

concurrency:
    group: dev-build-${{ github.event.workflow_run.head_sha || github.sha }}
    cancel-in-progress: false

jobs:
    dev-build:
        # Only on green CI, and never for the release-please merge commit (which would
        # publish a pointless rc for a version that was just released).
        if: >-
            github.event_name == 'workflow_dispatch' ||
            (github.event.workflow_run.conclusion == 'success' &&
             !contains(github.event.workflow_run.head_commit.message, 'chore: release'))
        runs-on: ubuntu-latest
        timeout-minutes: 10
        steps:
            - name: Checkout
              uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
              with:
                  ref: ${{ github.event.workflow_run.head_branch || github.ref_name }}
                  fetch-depth: 0

            - name: Compute next version
              id: nextver
              uses: TriPSs/conventional-changelog-action@952b14bbc4be87e8458a6ac5926fc655608b1b19 # v6
              with:
                  github-token: ${{ github.token }}
                  version-file: custom_components/sungrow/manifest.json
                  version-path: version
                  tag-prefix: v
                  preset: conventionalcommits
                  skip-on-empty: true
                  skip-version-file: true
                  skip-commit: true
                  skip-tag: true
                  skip-git-pull: true
                  git-push: false
                  output-file: false

            - name: Compute dev tag
              id: tag
              env:
                  SKIPPED: ${{ steps.nextver.outputs.skipped }}
                  NEXT_VERSION: ${{ steps.nextver.outputs.version }}
                  HEAD_BRANCH: ${{ github.event.workflow_run.head_branch || github.ref_name }}
                  WORKFLOW_EVENT: ${{ github.event.workflow_run.event || 'push' }}
                  RUN_ID: ${{ github.run_id }}
              run: |
                  # No releasable commits -> no bump; fall back to the current manifest version.
                  if [ "$SKIPPED" = "true" ] || [ -z "$NEXT_VERSION" ]; then
                    VERSION=$(jq -r .version custom_components/sungrow/manifest.json)
                  else
                    VERSION="$NEXT_VERSION"
                  fi
                  export VERSION HEAD_BRANCH WORKFLOW_EVENT RUN_ID
                  TAG=$(bash scripts/compute-dev-tag.sh)
                  echo "tag=${TAG}" >> "$GITHUB_OUTPUT"

            - name: Create pre-release
              env:
                  GH_TOKEN: ${{ github.token }}
                  TAG: ${{ steps.tag.outputs.tag }}
                  COMMIT_SHA: ${{ github.event.workflow_run.head_sha || github.sha }}
                  HEAD_BRANCH: ${{ github.event.workflow_run.head_branch || github.ref_name }}
              run: |
                  gh release create "$TAG" \
                    --prerelease \
                    --title "$TAG" \
                    --notes "Development build from \`${HEAD_BRANCH}\` at commit \`${COMMIT_SHA}\`."
```

- [ ] **Step 2: YAML parses**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/dev-build.yml')); print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/dev-build.yml
git commit -m "ci: add dev-build workflow (main rc + PR branch pre-releases)"
```

---

### Task 5: update PR-close cleanup for the new tag shape

**Files:**
- Modify: `.github/workflows/cleanup-pr-release.yml`

- [ ] **Step 1: Replace the tag-match block**

Replace the `run:` body's tag-prefix logic with a regex match (branch is now in the middle of
the tag). Change from `TAG_PREFIX="dev-${BRANCH}-v"` + `startswith` to:

```bash
          BRANCH=$(printf '%s' "$BRANCH_RAW" | sed 's/[^a-zA-Z0-9._-]/-/g' | sed 's/-\{2,\}/-/g')
          echo "Deleting dev builds for branch: ${BRANCH}"
          gh release list --limit 1000 --json tagName,isPrerelease \
            --jq ".[] | select(.isPrerelease and (.tagName | test(\"^dev-v[0-9]+\\\\.[0-9]+\\\\.[0-9]+-${BRANCH}-[0-9]+$\"))) | .tagName" \
          | while read -r tag; do
              echo "Deleting release and tag: ${tag}"
              gh release delete "$tag" --yes --cleanup-tag
            done
```

- [ ] **Step 2: Sanity-check the regex against sample tags**

```bash
BRANCH="feat-x"
printf '%s\n' "dev-v3.1.0-feat-x-123" "dev-v3.1.0-rc123" "dev-v3.1.0-feat-y-9" "v3.1.0" \
  | grep -E "^dev-v[0-9]+\.[0-9]+\.[0-9]+-${BRANCH}-[0-9]+$"
```
Expected: prints only `dev-v3.1.0-feat-x-123`.

- [ ] **Step 3: YAML parses + commit**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/cleanup-pr-release.yml')); print('ok')"
git add .github/workflows/cleanup-pr-release.yml
git commit -m "ci: match new dev-build tag shape in PR-close cleanup"
```

---

### Task 6: remove the superseded workflows

**Files:**
- Delete: `.github/workflows/release-pr.yml`, `.github/workflows/publish-release.yml`, `.github/workflows/release.yml`

- [ ] **Step 1: Delete and commit**

```bash
git rm .github/workflows/release-pr.yml .github/workflows/publish-release.yml .github/workflows/release.yml
git commit -m "ci: remove bespoke release/publish/dev workflows superseded by release-please"
```

- [ ] **Step 2: Confirm the remaining release-related workflows are coherent**

```bash
ls .github/workflows/ | grep -E "release|dev|cleanup"
# Expect: release-please.yml, dev-build.yml, cleanup-pr-release.yml, cleanup-dev-releases.yml
```

---

### Task 7: PR + migration one-offs

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin ci/release-please-migration
gh pr create --repo KRoperUK/sungrow-hass --base main --head ci/release-please-migration \
  --title "ci: migrate to release-please + three-tier dev builds" \
  --body "See docs/superpowers/specs/2026-07-04-release-please-migration-design.md. Closes the duplicate-release-PR bug (#130/#134). 🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 2: After this PR merges — close the stale custom release PRs and delete their branches**

```bash
gh pr close 130 --repo KRoperUK/sungrow-hass --comment "Superseded by the release-please migration."
gh pr close 134 --repo KRoperUK/sungrow-hass --comment "Superseded by the release-please migration."
git push origin --delete release/v3.0.1 release/v3.1.0
```

- [ ] **Step 3: Verify the first post-merge run**

After merge, confirm on GitHub Actions that **Release Please** opened a single rolling PR titled
`chore: release 3.1.0` (or the correct next version), and that a `dev-v…-rc…` pre-release was
published for the merge commit. Trigger CI on the release PR manually (close→reopen) so required
checks pass before merging it.

---

## Notes for the executor

- **Do not merge** this PR or the release-please PR without the maintainer — `main` is protected
  and they review/merge.
- Task 1 Step 3 (dry-run) is the one genuine unknown; resolve it before proceeding to Task 2.
- Steps that call the live GitHub API (Task 7) run only at/after merge; everything else is local.
- **`workflow_run` triggers only fire from the default branch.** `dev-build.yml` (and the removal
  of `release.yml`) therefore take effect only once this PR is on `main` — they will *not* run for
  this migration PR's own CI. Don't be alarmed that no dev build appears on this PR; verify the
  behaviour post-merge (Task 7 Step 3). release-please likewise starts on the first push to `main`
  after merge.
