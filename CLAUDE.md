# CLAUDE.md

Guidance for AI coding agents (Claude Code, etc.) working in this repository.

## What this is

A **Home Assistant custom integration** (`custom_components/sungrow`) that polls
Sungrow inverters via the **iSolarCloud** cloud API, using the
[`sungrow-isolarcloud`](https://pypi.org/project/sungrow-isolarcloud/) library — a
fork of `pysolarcloud`, imported as `pysolarcloud`. Distributed via HACS; quality
scale is **platinum**. `iot_class` is `cloud_polling`.

## Architecture

| File | Responsibility |
| --- | --- |
| `__init__.py` | Entry setup/unload. Builds `SungrowAuth` + `Plants`, creates one coordinator per plant, **persists rotated tokens back to the config entry**, classifies errors into `ConfigEntryNotReady` (transient) vs `ConfigEntryAuthFailed` (reauth). Registers the OAuth callback HTTP view. Owns the **EMS heartbeat** lifecycle (`async_start_heartbeat`/`async_stop_heartbeat`) and raises the `heartbeat_stopped` Repair when a heartbeat loop exits unexpectedly while dispatch is active — the #231 silent-death guard (#254). |
| `auth.py` | `SungrowAuth(pysolarcloud.Auth)` — adds a `token_updater` callback that fires when the access token is refreshed (pysolarcloud rotates the refresh token in memory). `AUTH_ERRORS` lists upstream error codes that mean "credentials dead". |
| `coordinator.py` | `SungrowPlantCoordinator(DataUpdateCoordinator)` — fetches realtime (and, when enabled, per-device) data per plant; reads the scan interval from `entry.options`; `is_auth_error()` maps exceptions to reauth vs retry. Also owns: **`has_battery`** gating (dispatch controls), an **availability grace window** (`_within_availability_grace`, `AVAILABILITY_GRACE_SECONDS=900`) so a single failed poll doesn't flap entities unavailable, **rate-limit back-off** (`is_rate_limit_error`, `_adjust_poll_backoff`, doubling up to `BACKOFF_MAX_INTERVAL=1h`), and **Repairs** (raises/clears the `whitelist_rejection` / `rate_limited` issues; only a *successful* poll clears them). Also always fetches each inverter/ESS device's operating status (points 29/13146) so the Fault sensor can show a reason (#182). |
| `config_flow.py` | **Two-phase setup:** the user step creates the hub entry from credentials (no tokens) and sets a unique ID (App ID); the token-less entry then raises `ConfigEntryAuthFailed`, so **authorization runs via reauth** (`async_step_reauth` → auto OAuth-callback wait with a manual code/URL fallback). Creating the hub first runs `async_setup`, which registers the callback view *before* any redirect (fixes the first-install 404). Also hosts the **reconfigure** flow (`async_step_reconfigure`, change region/credentials in place) and the **options** flow (`SungrowOptionsFlow`, polling interval, extra measure points, per-device sensors). A fourth transport **`cloud_user`** (#268) authenticates with a normal iSolarCloud account (email/password) via `pysolarcloud.UserAuth` — `async_step_cloud_user` validates the login, and `_async_setup_cloud_user` (isolated from the OAuth path, no `Control`) discovers plants; realtime data mapping is Phase 3 (#269). |
| `sensor.py` | Builds `SungrowSensor` (plant), `SungrowDeviceSensor` (per-device) and `SungrowPlantDetailSensor` (plant-level health/tariffs from getPowerStationDetail, #178) entities from the stored coordinators. `infer_device_class()` maps units → device/state class so the Energy dashboard works; `_DIAGNOSTIC_CODES` marks the diagnostic points. |
| `binary_sensor.py` | Per-device `SungrowDeviceFaultBinarySensor` (PROBLEM, from `dev_fault_status`; exposes an `operating_status` reason attribute, #182) and `SungrowDeviceConnectivityBinarySensor` (CONNECTIVITY, from `dev_status`, exposes commissioning date). |
| `number.py` / `select.py` | Dispatch **Number**/**Select** entities (charge/discharge, SOC limits, forced charging, export/active-power limits, reactive power). `battery_only` params are gated on `coordinator.has_battery` (#148); power sliders are sized to the device's rated power. Write-only controls set `assumed_state` (no API read-back). `select.py` owns the EMS heartbeat lifecycle, and after a Charge/Discharge write **verifies actuation** by reading Energy Management Mode (10003) back — retrying the forced-mode write once, then raising the `dispatch_not_actuated` Repair if the inverter never left Self-consumption (#254, Confirm→Retry→Notify). |
| `const.py` | Domain, config keys, `GATEWAYS`, scan-interval defaults, and the per-device point maps (`INVERTER_DIAGNOSTIC_POINTS`, `BATTERY_DEVICE_POINTS`, `METER_DEVICE_POINTS`, `COMM_MODULE_POINTS`, operating-status points). |
| `measure_points.py` / `measure_points_data.py` | English naming (`resolve_name`, `CODE_ALIASES`), unit/code classification (`resolve_classification`, `normalize_unit`) and enum resolution, grounded in the official iSolarCloud measure-point catalogs. `measure_points_data.py` holds the catalog rows, enum tables and aliases. |
| `__init__.py` helpers | `build_device_info()` (model/serial/manufacturer device cards), `_async_has_battery` (battery detection), `PLATFORMS = [BINARY_SENSOR, NUMBER, SELECT, SENSOR]`. |

### Critical invariant — token persistence

`pysolarcloud.Auth.async_get_access_token()` refreshes the access token when it
expires and **assigns a brand-new `tokens` dict containing a rotated refresh
token**. If the new tokens are not written back to the config entry, the next
Home Assistant restart reloads an invalidated refresh token and every entity goes
unavailable (the historical bug behind issues #14/#15/#20/#21). The
`token_updater` callback wired in `__init__.py.async_setup_entry` is what keeps
this working — **do not remove it**, and keep `sungrow-isolarcloud` pinned in
`manifest.json` (and `requirements_test.txt`).

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
- `strings.json` and **every** `translations/*.json` must stay in sync (a test
  enforces key parity across all languages, not just `en`).

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
