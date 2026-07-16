# GitHub Copilot instructions

This repo is a **Home Assistant custom integration** for Sungrow inverters via the
iSolarCloud cloud API (`iot_class: cloud_polling`), built on the
**`sungrow-isolarcloud`** library (a fork of `pysolarcloud`, imported as
`pysolarcloud`). Distributed via HACS; quality scale is **platinum**. Code lives in
`custom_components/sungrow/`. `CLAUDE.md` is the fuller architecture guide.

## Module map

- `__init__.py` — entry setup/unload; builds auth + one coordinator per plant;
  **persists rotated tokens** to the config entry; classifies errors into
  `ConfigEntryNotReady` (retry) vs `ConfigEntryAuthFailed` (reauth); registers the
  OAuth callback HTTP view; owns `build_device_info`, battery detection, and the EMS
  heartbeat helpers (which raise the `heartbeat_stopped` Repair if a heartbeat loop
  dies unexpectedly while dispatch is active, #254). `PLATFORMS = [BINARY_SENSOR, NUMBER, SELECT, SENSOR]`.
- `auth.py` — `SungrowAuth(pysolarcloud.Auth)` with a `token_updater` callback;
  `AUTH_ERRORS` lists upstream "credentials dead" codes.
- `coordinator.py` — `SungrowPlantCoordinator`; realtime + per-device fetch;
  `is_auth_error()`/`is_rate_limit_error()`; availability grace window, rate-limit
  back-off, and Repairs (`whitelist_rejection` / `rate_limited`).
- `config_flow.py` — two-phase setup (hub entry first, then authorize via **reauth**),
  a **reconfigure** flow, and options (polling interval, extra measure points, per-device
  sensors). Also a **`cloud_user`** transport (#268): a `UserAuth` email/password app/web
  login (unofficial), set up via `_async_setup_cloud_user`; Phase 2 authenticates + lists
  plants, realtime data is Phase 3 (#269).
- `sensor.py` — `SungrowSensor` (plant) + `SungrowDeviceSensor` (per-device) +
  `SungrowPlantDetailSensor`; `infer_device_class()` maps units → device/state class.
- `binary_sensor.py` — per-device Fault (PROBLEM, exposes an `operating_status` reason)
  and Connectivity binary sensors.
- `number.py` / `select.py` — dispatch controls (charge/discharge, SOC limits, forced
  charging, export/power limits, reactive power). `battery_only` params gate on
  `coordinator.has_battery`; `select.py` owns the EMS-heartbeat lifecycle and, after a
  Charge/Discharge write, verifies actuation by reading Energy Management Mode back
  (retry-once, then the `dispatch_not_actuated` Repair) — #254.
- `const.py` — domain, config keys, gateways, scan-interval defaults, per-device point
  maps (`INVERTER_DIAGNOSTIC_POINTS`, `BATTERY_DEVICE_POINTS`, …).
- `measure_points.py` / `measure_points_data.py` — English naming, unit/code
  classification, and enum resolution, grounded in the official iSolarCloud catalogs.
- `diagnostics.py` — redacted config-entry diagnostics.

## Must-follow rules

1. **Never drop token persistence.** `pysolarcloud` rotates the refresh token in memory
   on refresh; the `token_updater` callback writes it back to the config entry. Removing
   it reintroduces the "entities unavailable after reboot" bug (#14/#15/#20/#21).
2. **Keep the library pinned** as `sungrow-isolarcloud==X.Y.Z` in `manifest.json` **and**
   `requirements_test.txt`.
3. **Keep `strings.json` and every `translations/*.json` in sync** — a test enforces key
   parity across all languages.
4. **Add/update tests** for any behaviour change. Tests mock `SungrowAuth` and `Plants`
   (see `tests/conftest.py`).
5. **Conventional Commits** for commit and PR titles (`fix:`/`feat:`/`chore:`/`docs:`) —
   this drives release-please and the changelog.
6. **`main` is protected** — work on a branch and open a PR. Required checks: `lint`,
   `test`, `hacs_validate`.

## Local checks (match CI)

```bash
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
mypy                       # strict; the CI test job runs this — ruff+pytest alone isn't enough
python -m pytest tests/    # keep coverage above the pyproject fail_under
```

Style: **Python 3.13** (HA requires >=3.13), ruff line length 120, mypy `strict = true`.
See `docs/TROUBLESHOOTING.md` for user-facing auth/setup guidance.
