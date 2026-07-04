# Release-Please Migration + Three-Tier Dev Builds — Design

**Goal:** Replace the bespoke `conventional-changelog-action` + `create-pull-request` release
system with **release-please** for real releases, and formalise a three-tier dev pre-release
scheme (branch builds, main RC builds) whose versions track the next release-please version.

**Architecture:** release-please owns the rolling release PR + version bump + tag + GitHub
release on `main`. A single `dev-build.yml` (triggered by CI success) publishes dev pre-releases
for `main` (rc) and for PR branches. Cleanup is event-driven (PR close, full release) with a
weekly backstop sweep.

**Tech stack:** GitHub Actions, `googleapis/release-please-action@v4`,
`TriPSs/conventional-changelog-action@v6` (dev-build version prediction only), `gh` CLI.

## Global Constraints

- Actions are **pinned by commit SHA** (existing repo convention) — keep pinning new actions.
- Never inline `${{ }}` expressions into `run:` shell scripts; pass via `env:` and reference as
  shell vars (existing convention — shell-injection safety, esp. branch names).
- `main` is protected; required checks `lint`, `test`, `hacs_validate`.
- Dev builds must be GitHub **pre-releases** so HACS surfaces them.
- Tag prefix keeps the `v`: real `vX.Y.Z`; dev `dev-vX.Y.Z-...`.

---

## Current state (what we're replacing)

- `release-pr.yml` — computes next version (`conventional-changelog-action`) and opens a release
  PR on branch **`release/v<version>`**. **Bug:** the version-in-branch-name means a changed
  computed version (e.g. patch→minor after a `feat`) opens a *new* PR, orphaning the old one
  (#130 `release/v3.0.1` and #134 `release/v3.1.0` are both open).
- `publish-release.yml` — on merged release PR (`release` label + head `release/v*`): extracts
  version from `manifest.json`, creates tag + GitHub release, deletes main dev pre-releases.
- `release.yml` ("Dev Release (main)") — on CI success on `main`: computes next version, publishes
  `dev-v<version>-<run_number>` pre-release.
- `cleanup-pr-release.yml` — on PR close: deletes `dev-<branch>-v*` pre-releases (note: no current
  workflow *creates* branch builds — this scheme adds them).
- `cleanup-dev-releases.yml` — weekly 30-day sweep of all `dev-*` pre-releases (kept).

CI (`ci.yml`, `name: CI`) triggers on `push: [main]` and `pull_request: [main]` — **not** on
branch pushes. Current released version: `v3.0.0` (tag exists; `manifest.json` = `3.0.0`).

---

## Target architecture

| Workflow | Fate | Trigger | Responsibility |
| --- | --- | --- | --- |
| `release-please.yml` | **new** | `push: [main]` | Rolling release PR (bumps `manifest.json`, `pyproject.toml`, `CHANGELOG.md`); on PR merge → tag `vX.Y.Z` + GitHub release; on release → prune all `dev-*`. |
| `dev-build.yml` | **new** (absorbs `release.yml`) | `workflow_run: [CI] completed` | Publish dev pre-release: `main`→rc, PR branch→branch build. |
| `cleanup-pr-release.yml` | **update** | `pull_request: closed` | Delete this branch's `dev-v<ver>-<branch>-*` builds. |
| `cleanup-dev-releases.yml` | **keep** | weekly cron | 30-day backstop sweep of `dev-*`. |
| `release-pr.yml` | **delete** | — | replaced by release-please |
| `publish-release.yml` | **delete** | — | replaced by release-please |
| `release.yml` | **delete** | — | folded into `dev-build.yml` |

### Tag formats

| Kind | Format | Example |
| --- | --- | --- |
| Real release | `vX.Y.Z` | `v3.1.0` |
| Main RC | `dev-v<next>-rc<run_id>` | `dev-v3.1.0-rc17482913` |
| PR branch | `dev-v<next>-<branch>-<run_id>` | `dev-v3.1.0-feat-clearer-errors-17482913` |

`<next>` = version predicted by `conventional-changelog-action` (same conventional-commits →
semver rules release-please uses). `<run_id>` = `github.run_id` of the dev-build run (globally
unique). `<branch>` = `github.event.workflow_run.head_branch`, sanitised
`sed 's/[^a-zA-Z0-9._-]/-/g; s/-\{2,\}/-/g'` (matches existing cleanup sanitisation).

---

## Component detail

### 1. `release-please.yml`

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
    steps:
      - uses: googleapis/release-please-action@<pinned-sha>  # v4
        id: release
        with:
          token: ${{ github.token }}          # default token (CI on the PR is triggered manually)
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json
      - name: Prune all dev pre-releases on a real release
        if: ${{ steps.release.outputs.release_created == 'true' }}
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
        run: |
          gh release list --limit 1000 --json tagName,isPrerelease \
            --jq '.[] | select(.isPrerelease and (.tagName | startswith("dev-"))) | .tagName' \
          | while read -r tag; do gh release delete "$tag" --yes --cleanup-tag; done
```

**Config files (repo root):**

`.release-please-manifest.json`
```json
{ ".": "3.0.0" }
```

`release-please-config.json`
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

> **Config to validate at implementation time** (run `npx release-please … --dry-run` locally):
> that `release-type: simple` doesn't require a `version.txt` alongside `extra-files`, and that
> the `toml` extra-file updater accepts `$.project.version`. If `simple` insists on `version.txt`,
> fall back to a `generic` updater with an inline `x-release-please-version` annotation, or add a
> tiny `version.txt`. Confirm the tag format is `vX.Y.Z` (default `include-v-in-tag: true`).

**Operational note (CI on the release PR):** a PR opened by the default `GITHUB_TOKEN` does not
trigger CI (GitHub anti-recursion), so required checks sit pending. **Accepted:** the maintainer
triggers CI on the release PR manually (close→reopen the PR, or push an empty commit) before
merging. No PAT/App token is introduced.

### 2. `dev-build.yml`

```yaml
name: Dev Build
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
permissions:
  contents: write
concurrency:
  group: dev-build-${{ github.event.workflow_run.head_sha }}
  cancel-in-progress: false
jobs:
  dev-build:
    if: >-
      github.event.workflow_run.conclusion == 'success' &&
      !contains(github.event.workflow_run.head_commit.message, 'chore: release')
    runs-on: ubuntu-latest
    steps:
      - checkout ref: ${{ github.event.workflow_run.head_branch }}  # branch tip; fetch-depth: 0
      - id: nextver: conventional-changelog-action (compute-only, as in current release.yml)
      - id: tag: build the tag (see logic below) via env-passed values
      - create pre-release with gh (prerelease, title = tag)
```

**Tag logic** (all context via `env:`, referenced as shell vars):
```bash
# VERSION = nextver.version, or current manifest version when skipped (no releasable commits)
if [ "$HEAD_BRANCH" = "main" ] && [ "$WORKFLOW_EVENT" = "push" ]; then
  TAG="dev-v${VERSION}-rc${RUN_ID}"
  NOTES="Release-candidate build from main at ${HEAD_SHA}."
else
  BRANCH=$(printf '%s' "$HEAD_BRANCH" | sed 's/[^a-zA-Z0-9._-]/-/g' | sed 's/-\{2,\}/-/g')
  TAG="dev-v${VERSION}-${BRANCH}-${RUN_ID}"
  NOTES="PR build for branch ${HEAD_BRANCH} at ${HEAD_SHA}."
fi
```

- CI runs on `pull_request` for branches → `workflow_run.event == 'pull_request'`, `head_branch`
  = the PR source branch → branch build. CI runs on `push` for `main` → rc build.
- The `chore: release` guard skips a pointless rc right after a release PR merges.
- Branch builds therefore only occur once a PR exists (CI's `pull_request` trigger); pushes to a
  branch with no PR never build — which keeps them tied to the PR-close cleanup.

### 3. `cleanup-pr-release.yml` (update)

On `pull_request: closed`, delete this branch's builds. New tag shape puts the branch in the
middle, so match by regex with the known sanitised branch:
```bash
BRANCH=$(printf '%s' "$BRANCH_RAW" | sed 's/[^a-zA-Z0-9._-]/-/g' | sed 's/-\{2,\}/-/g')
gh release list --limit 1000 --json tagName,isPrerelease \
  --jq ".[] | select(.isPrerelease and (.tagName | test(\"^dev-v[0-9]+\\\\.[0-9]+\\\\.[0-9]+-${BRANCH}-[0-9]+$\"))) | .tagName" \
| while read -r tag; do gh release delete "$tag" --yes --cleanup-tag; done
```

### 4. `cleanup-dev-releases.yml` (keep as-is)

Weekly 30-day sweep of all `dev-*` pre-releases — unchanged; remains the backstop for orphans
(e.g. a branch closed without merge whose event was missed).

---

## Migration steps (one-offs)

1. Add `release-please-config.json` + `.release-please-manifest.json` (seed `3.0.0`).
2. Add `release-please.yml` and `dev-build.yml`; delete `release-pr.yml`, `publish-release.yml`,
   `release.yml`; update `cleanup-pr-release.yml`.
3. Close the stale custom release PRs **#130** and **#134** and delete their `release/v*` branches.
4. First push to `main` after merge: release-please opens its rolling PR for the next version
   (≥ `3.1.0` given the pending `feat` commits).

---

## Validation approach (before trusting it on `main`)

- **release-please config:** run `npx release-please release-pr --dry-run --token=… --repo-url=KRoperUK/sungrow-hass`
  (or the manifest command) locally/CI to confirm the computed version and that `manifest.json`
  + `pyproject.toml` are the files it would update. No PR is created in dry-run.
- **dev-build tag logic:** unit-test the bash tag builder with a tiny shell harness (feed sample
  `HEAD_BRANCH`/`VERSION`/`RUN_ID`, assert the tag string) so main-vs-branch + sanitisation are
  covered without publishing releases.
- **End-to-end:** exercise on a throwaway PR branch first (confirm a `dev-v…-<branch>-…`
  pre-release appears and is deleted on close) before relying on the `main` rc + release path.

## Out of scope

- Signing / provenance of releases.
- Changing the HACS metadata or `hacs.json`.
- Reformatting the historical `CHANGELOG.md` (release-please prepends going forward; old sections
  remain in their existing format).
