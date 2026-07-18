# Contributing

Thanks for your interest in improving the Sungrow iSolarCloud integration!

By participating in this project, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please report unacceptable behavior
privately to the maintainer as described there — do not open a public issue.

## Development setup

This project targets Python 3.13 and Home Assistant's custom-component test
harness. Using [uv](https://docs.astral.sh/uv/) is the quickest path:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements_test.txt
```

(Any virtualenv with `pip install -r requirements_test.txt` works too.)

## Before you push

Run the same checks CI runs:

```bash
.venv/bin/ruff check custom_components/
.venv/bin/ruff format --check custom_components/
.venv/bin/python -m pytest tests/
```

- Tests must pass and coverage must stay at or above the configured threshold
  (`fail_under` in `pyproject.toml`).
- Live tests (marked `live`) are skipped by default and require real iSolarCloud
  credentials in a `.env` file (see `.env.example`).
- A `.pre-commit-config.yaml` is provided; `pre-commit install` will run ruff on
  commit.

## Pull requests

- Use **Conventional Commits** for PR titles and commits (`fix:`, `feat:`,
  `chore:`, `docs:` …). This drives automated changelog and version bumps. Use
  `feat!:` or a `BREAKING CHANGE:` footer for changes that bump the major version
  (e.g. anything that changes entity IDs).
- Keep changes focused; add or update tests for any behaviour change.
- All PRs target `main` and must pass CI (lint, tests, HACS + hassfest validation)
  and at least one review before merge.

### Code review

`main` is protected and the project is largely single-maintainer, so a second pair
of eyes matters — especially since many PRs are AI-assisted.

- Request a **Copilot code review** (or a second human reviewer, if available) on any
  non-trivial PR. Treat AI-generated changes the same: they still get a review pass.
- Pay particular attention to changes touching the high-risk areas: `auth.py`,
  `config_flow.py`, `coordinator.py`, and the dispatch controls (`number.py` /
  `select.py`). These are behind the historical token-persistence and dispatch bugs.
- A PR that changes the integration also mints a **dev pre-release** you can install
  via HACS to validate on real hardware before merging — see below.

## Releasing

Releases are automated with [release-please](https://github.com/googleapis/release-please);
you should never hand-edit the version or changelog. This section documents the flow so
anyone (not just the original maintainer) can cut a release or recover from a bad one.

### Cutting a stable release

1. Merge Conventional-Commit PRs to `main` as normal.
2. release-please keeps an open **`chore(main): release X.Y.Z`** PR that accumulates the
   changelog and the next version. Review it, then **merge it** to publish.
3. On that merge, `release-please.yml` tags the release, packages **`sungrow.zip`** and
   attaches it (HACS `zip_release`), **verifies the asset is present** (guards the #245
   regression), and prunes the dev/RC pre-releases.

The version is bumped from the commit types since the last release: `fix:` → patch,
`feat:` → minor, `feat!:` / `BREAKING CHANGE:` → major.

### Dev & RC pre-releases (testing builds)

`main` is never mutated for pre-releases — they point at a *synthetic* commit that only
rewrites the version files, so HACS shows a real, sortable version.

- **Per-PR dev builds** — a component-touching PR, once CI is green, publishes
  `vX.Y.Z-pr.<pr>.<run>` (see the `dev-release` job in `ci.yml`). Install it via HACS
  (enable pre-releases) to test the PR on real hardware. This is the main way to get
  hybrid/three-phase models validated, since the maintainer's rig can't cover them.
- **RC builds on `main`** — a push to `main` publishes `vX.Y.Z-rc.N` (`dev-release-main`).

### Recovering from a bad release

- **Missing `sungrow.zip` (HACS install fails, à la #245):** the post-release verify step
  now fails loudly, but if a release ever ships broken, land a `fix:` commit and merge the
  next release-please PR to cut a patch — do **not** hand-edit the tag.
- **Bad code shipped:** revert on `main` with a `fix:`/`revert:` PR and release again;
  avoid deleting published release tags (HACS clients may already have them).
- Re-apply branch protection with `scripts/setup-branch-protection.sh` if it drifts.

## Project layout

| Path | Purpose |
| --- | --- |
| `custom_components/sungrow/__init__.py` | Entry setup, token persistence, error handling |
| `custom_components/sungrow/auth.py` | Token-persisting `Auth` wrapper |
| `custom_components/sungrow/coordinator.py` | Per-plant `DataUpdateCoordinator` |
| `custom_components/sungrow/config_flow.py` | Config, reauth, and options flows |
| `custom_components/sungrow/sensor.py` | Sensor entities + device-class inference |
| `tests/` | Unit/integration tests |

## Reporting issues

Please use the issue templates and read
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) first.
