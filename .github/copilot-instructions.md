# GitHub Copilot instructions

This repo is a **Home Assistant custom integration** for Sungrow inverters via the
iSolarCloud cloud API (`iot_class: cloud_polling`), built on the
**`sungrow-isolarcloud`** library (a fork of `pysolarcloud`, imported as
`pysolarcloud`). Distributed via HACS; quality scale is **platinum**. Code lives in
`custom_components/sungrow/`. `CLAUDE.md` is the fuller architecture guide.

## Transports

One entity/platform layer, three transports selected by `CONF_TRANSPORT`: `cloud_only`
(default, developer OAuth app), `cloud_user` (#268, unofficial account email/password),
`modbus_only` (#159, cloud-free local Modbus to a WiNet-S dongle). `cloud_modbus` was
retired in #348.

## Module map

- `__init__.py` — entry setup/unload for all transports; builds auth + one coordinator
  per plant; **persists rotated tokens** to the config entry; classifies errors into
  `ConfigEntryNotReady` (retry) vs `ConfigEntryAuthFailed` (reauth); registers the
  OAuth callback HTTP view and the plant "service" devices; registers the integration's
  **services once for every transport** (#411); owns battery detection. `PLATFORMS = [BINARY_SENSOR, NUMBER, SELECT, SENSOR]`.
- `auth.py` — `SungrowAuth(pysolarcloud.Auth)` with a `token_updater` callback;
  `AUTH_ERRORS` lists upstream "credentials dead" codes.
- `heartbeat.py` — EMS heartbeat loop; raises the `heartbeat_stopped` Repair if a
  heartbeat dies unexpectedly while dispatch is active (#254).
- `coordinator.py` — `SungrowPlantCoordinator`; realtime + per-device fetch;
  `is_auth_error()`/`is_rate_limit_error()`; availability grace window, rate-limit
  back-off, Repairs (`whitelist_rejection` / `rate_limited`), and the derived daily
  baselines (`daily_yield`, plus grid import/export, #223/#400/#471).
- `device_helpers.py` — device-registry helpers. Nest devices with **`via_device_id`**
  (the parent's registry device id; `via_device` is deprecated, #407) and use
  `build_device_info_for()`; open-coding nesting caused #383.
- `entity_platform_helpers.py` — `create_entity_adder()`: per-entry unique_id dedup,
  cross-entry collision skip, and the listener that adds devices/points at runtime.
- `config_flow.py` + `_config_flow/` — per-transport package (#354). Two-phase setup
  (hub entry first, then authorize via **reauth**), a **reconfigure** flow, options
  (polling interval, extra measure points, per-device sensors), the WiNet-S discovery
  wizard and the `cloud_user` (#268) login. `zeroconf.py` only refreshes the host of a
  **discovery-managed** entry, never a user-set one (#402/#414).
- `sensor.py` — `SungrowSensor` (plant) + `SungrowDeviceSensor` (per-device) +
  `SungrowPlantDetailSensor`; classification comes from `measure_points`.
- `binary_sensor.py` — per-device Fault (PROBLEM, exposes an `operating_status` reason)
  and Connectivity binary sensors.
- `number.py` / `select.py` / `schedule.py` — dispatch controls (charge/discharge, SOC
  limits, forced charging, export/power limits, reactive power). `battery_only` params
  gate on `coordinator.has_battery`; `select.py` owns the EMS-heartbeat lifecycle, the
  forced-dispatch auto-revert, and actuation verification (read Energy Management Mode
  back; retry-once, then the `dispatch_not_actuated` Repair) — #254. `schedule.py` runs
  daily charge/discharge windows (#359).
- `services.py` — `backfill`, `set_battery_mode`, `refresh_tokens` (+ schemas).
- `modbus.py` / `modbus_registers.py` / `modbus_control.py` — local Modbus client,
  register maps/decoders, and holding-register dispatch. MPPT points are model-gated
  (#398); absent-meter points are suppressed (#387).
- `derived_daily.py` / `migration.py` / `energy_units.py` / `user_realtime.py` /
  `model_specs.py` / `model_capabilities.py` — derived daily energy, config-entry
  migration (v1→v6), unit normalisation + source tagging, the `cloud_user` point mapper,
  and per-model datasheet metadata/family resolution.
- `const.py` — domain, config keys, gateways, scan-interval defaults, per-device point
  maps (`INVERTER_DIAGNOSTIC_POINTS`, `BATTERY_DEVICE_POINTS`, …).
- `measure_points.py` / `measure_points_data.py` — English naming, unit/code
  classification, and enum resolution, grounded in the official iSolarCloud catalogs.
- `diagnostics.py` — redacted config-entry diagnostics (incl. `modbus_diagnostics`).

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
7. **Commits must be signed** — "Require signed commits" is enabled on `main`, so an
   unsigned commit cannot be merged. Configure a key once (`user.signingkey` +
   `commit.gpgsign true`).
8. **Keep the docs honest** — if you change behaviour, update the matching doc
   (`docs/local-modbus.md`, `docs/SENSORS.md`, `docs/TROUBLESHOOTING.md`, `README.md`)
   and `quality_scale.yaml` (which must reflect reality, e.g. services *are* registered).

## Local checks (match CI)

```bash
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
mypy                       # strict; the CI test job runs this — ruff+pytest alone isn't enough
python -m pytest tests/    # keep coverage above the pyproject fail_under
```

Style: **Python 3.14** (HA requires >=3.14), ruff line length 120, mypy `strict = true`.
See `docs/TROUBLESHOOTING.md` for user-facing auth/setup guidance.
