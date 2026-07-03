# CLAUDE.md

Guidance for AI coding agents (Claude Code, etc.) working in this repository.

## What this is

A **Home Assistant custom integration** (`custom_components/sungrow`) that polls
Sungrow inverters via the **iSolarCloud** cloud API, using the
[`pysolarcloud`](https://pypi.org/project/pysolarcloud/) library. Distributed via
HACS. `iot_class` is `cloud_polling`.

## Architecture

| File | Responsibility |
| --- | --- |
| `__init__.py` | Entry setup/unload. Builds `SungrowAuth` + `Plants`, creates one coordinator per plant, **persists rotated tokens back to the config entry**, classifies errors into `ConfigEntryNotReady` (transient) vs `ConfigEntryAuthFailed` (reauth). Registers the OAuth callback HTTP view. |
| `auth.py` | `SungrowAuth(pysolarcloud.Auth)` — adds a `token_updater` callback that fires when the access token is refreshed (pysolarcloud rotates the refresh token in memory). `AUTH_ERRORS` lists upstream error codes that mean "credentials dead". |
| `coordinator.py` | `SungrowPlantCoordinator(DataUpdateCoordinator)` — fetches realtime data per plant; reads the scan interval from `entry.options`; `is_auth_error()` maps exceptions to reauth vs retry. |
| `config_flow.py` | **Two-phase setup:** the user step creates the hub entry from credentials (no tokens) and sets a unique ID (App ID); the token-less entry then raises `ConfigEntryAuthFailed`, so **authorization runs via reauth** (`async_step_reauth` → auto OAuth-callback wait with a manual code/URL fallback). Creating the hub first runs `async_setup`, which registers the callback view *before* any redirect (fixes the first-install 404). Also hosts the **options** flow (`SungrowOptionsFlow`, polling interval). |
| `sensor.py` | Builds `SungrowSensor` entities from the stored coordinators. `infer_device_class()` maps units → device/state class so the Energy dashboard works. |
| `const.py` | Domain, config keys, `GATEWAYS`, scan-interval defaults. |

### Critical invariant — token persistence

`pysolarcloud.Auth.async_get_access_token()` refreshes the access token when it
expires and **assigns a brand-new `tokens` dict containing a rotated refresh
token**. If the new tokens are not written back to the config entry, the next
Home Assistant restart reloads an invalidated refresh token and every entity goes
unavailable (the historical bug behind issues #14/#15/#20/#21). The
`token_updater` callback wired in `__init__.py.async_setup_entry` is what keeps
this working — **do not remove it**, and keep `pysolarcloud` pinned in
`manifest.json`.

## Commands

```bash
# Environment (uv recommended; any Py3.13 venv works)
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements_test.txt

# Lint, type-check, format, test (mirror CI)
.venv/bin/ruff check custom_components/ tests/
.venv/bin/ruff format --check custom_components/ tests/
.venv/bin/mypy
.venv/bin/python -m pytest tests/

# Live tests (need real creds in .env; skipped otherwise)
.venv/bin/python -m pytest -m live
```

Coverage threshold (`fail_under`) is set in `pyproject.toml`; keep it green.

## Conventions

- **Python 3.13** (Home Assistant requires >=3.13), ruff (line length 120) for lint + format.
- **Conventional Commits** for commit and PR titles (`fix:`, `feat:`, `chore:`,
  `docs:`) — this drives changelog and version bumps.
- Every behaviour change needs tests. Tests mock `pysolarcloud` (`SungrowAuth`,
  `Plants`) — see `tests/conftest.py` fixtures (`mock_setup_auth`,
  `mock_plants_service`, `mock_auth`).
- `strings.json` and `translations/en.json` must stay in sync (a test enforces
  the strings shape).

## Workflow / repo rules

- **`main` is protected**: open a feature branch and a PR; do not push to `main`.
  Required checks: `lint`, `test`, `hacs_validate`. Re-apply rules with
  `scripts/setup-branch-protection.sh`.
- Releases are cut via `release-pr.yml` → merge → `publish-release.yml`.
  `release.yml` only moves a lightweight `dev-v*` tag (it must **not** commit to
  `main`).

## User-facing docs

`docs/TROUBLESHOOTING.md` is the first stop for auth/setup/"unavailable" reports;
keep it current when changing the auth or setup flow.
