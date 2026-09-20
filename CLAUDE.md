# CLAUDE.md

Guidance for AI coding agents (Claude Code, etc.) working in this repository.

## What this is

A **Home Assistant custom integration** (`custom_components/sungrow`) that reads
Sungrow inverters via the **iSolarCloud** cloud API, using the
[`sungrow-isolarcloud`](https://pypi.org/project/sungrow-isolarcloud/) library — a
fork of `pysolarcloud`, imported as `pysolarcloud`. Distributed via HACS; quality
scale is **platinum**. `iot_class` is `cloud_polling`.

Three transports share one entity/platform layer, chosen at config time by
`CONF_TRANSPORT`:

| Transport | Auth | Realtime source |
| --- | --- | --- |
| `cloud_only` (default) | Developer OAuth app (`SungrowAuth` + `Control`) | `Plants.async_get_realtime_data` |
| `cloud_user` (#268) | iSolarCloud account email/password, unofficial (`UserAuth` + `UserControl`) | `getPsDetail` / device list |
| `modbus_only` (#159) | none (cloud-free) | local Modbus TCP to a WiNet-S dongle |

`cloud_modbus` was retired in #348 and is migrated to `cloud_only`.

## Architecture

Grouped by subsystem; every file under `custom_components/sungrow/`.

**Entry lifecycle**

| File | Responsibility |
| --- | --- |
| `__init__.py` | Entry setup/unload for all three transports. Builds `SungrowAuth` + `Plants`, creates one coordinator per plant, **persists rotated tokens back to the config entry**, classifies errors into `ConfigEntryNotReady` (transient) vs `ConfigEntryAuthFailed` (reauth). Registers the OAuth callback HTTP view and the plant "service" devices (recording each plant device's registry id on the coordinator for nesting). Registers the integration's **services once, for every transport** (#411). Owns the **EMS heartbeat** lifecycle (`async_start_heartbeat`/`async_stop_heartbeat`) and raises the `heartbeat_stopped` Repair when a heartbeat loop exits unexpectedly while dispatch is active — the #231 silent-death guard (#254). `PLATFORMS = [BINARY_SENSOR, NUMBER, SELECT, SENSOR]`. |
| `auth.py` | `SungrowAuth(pysolarcloud.Auth)` — adds a `token_updater` callback that fires when the access token is refreshed (pysolarcloud rotates the refresh token in memory). `AUTH_ERRORS` lists upstream error codes that mean "credentials dead". |
| `heartbeat.py` | EMS heartbeat loop (`async_start_heartbeat`/`async_stop_heartbeat`): keeps the inverter in External-EMS mode while a forced charge/discharge is active. Extracted from `__init__.py` (#289). |
| `migration.py` | `async_migrate_entry` version chain (v1→v6): scan-interval units, transport renames, legacy-entity sweeps, yield-code renames, `app_id` back-fill (OAuth transports only — **not** `cloud_user`/`modbus_only`, whose unique_ids are not app ids). |
| `device_helpers.py` | Device-registry helpers. `build_device_info()` / `build_device_info_for()` build device cards and nest them with **`via_device_id`** (the parent's registry device id — `via_device` is deprecated and removed in HA 2027.8, #407); `find_related_cloud_plant_device_id()` links a local entry to the cloud plant owning the same serial; `unique_id_owned_by_other_entry()` skips cross-entry duplicate entities (#347). Every platform must nest through these helpers — open-coding is what caused #383. |
| `entity_platform_helpers.py` | `create_entity_adder()` — the shared per-platform boilerplate: per-entry `unique_id` dedup, cross-entry collision skip, and the coordinator listener that adds devices/points appearing after setup. |
| `oauth_view.py` | The `/api/sungrow_hass/callback` HTTP view that receives the OAuth redirect and resumes the config flow by `state`. |
| `diagnostics.py` | Redacted config-entry diagnostics: coordinator data, per-device points, `points_catalog`, and the local `modbus_diagnostics` (family, skipped blocks, `meter_present`, `dropped_mppt_points`). |
| `_serialization.py` | Shared JSON-safe coercion for diagnostics/serialisation. |

**Polling & data**

| File | Responsibility |
| --- | --- |
| `coordinator.py` | `SungrowPlantCoordinator(DataUpdateCoordinator)` — fetches realtime (and, when enabled, per-device) data per plant; reads the scan interval from `entry.options`; `is_auth_error()` maps exceptions to reauth vs retry. Also owns: **`has_battery`** gating (dispatch controls), an **availability grace window** (`_within_availability_grace`, `AVAILABILITY_GRACE_SECONDS=900`) so a single failed poll doesn't flap entities unavailable, **rate-limit back-off** (`is_rate_limit_error`, `_adjust_poll_backoff`, doubling up to `BACKOFF_MAX_INTERVAL=1h`), **Repairs** (raises/clears the `whitelist_rejection` / `rate_limited` issues; only a *successful* poll clears them), and the **derived daily baselines** (`daily_yield`, plus grid import/export whenever the lifetime counters are present) for families whose daily registers can't be trusted (#223/#400/#471). Always fetches each inverter/ESS device's operating status (points 29/13146) so the Fault sensor can show a reason (#182). |
| `daily_yield.py` | Derives a calendar-day figure from a lifetime counter as `total − start-of-day baseline` for the two register families whose own daily register can't be trusted. `step_daily_yield()` / `apply_derived_daily_yield()` do local `daily_yield` (SG-RS/SG-RT wire 5002 never resets at midnight); `apply_derived_daily_grid_energy()` / `DerivedDailyEnergyState` do local `daily_imported_energy` / `daily_exported_energy` from the lifetime grid counters (#471) — that derivation **replaces** the raw register whenever the lifetime counter exists, so the source is stable and `measure_points` can classify it ENERGY/TOTAL_INCREASING, seeding the first sample from the device's own daily figure (`first_anchor`) so the takeover doesn't zero the day; it derives nothing without a lifetime total. A **missing/zero/negative baseline re-anchors** to the current total, so a 0 at the day boundary can't publish a lifetime total as "today" (#400). |
| `backfill.py` | Cloud-only backfill engine: resolves series, chunks the window, and imports hourly statistics for cumulative-energy/power points. |
| `energy_units.py` | Unit normalisation for payloads (Wh→kWh, and kW→W for the `cloud_user` transport) plus `source` provenance tagging. |
| `user_realtime.py` | Maps the `cloud_user` transport's `getPsDetail` / device-list payloads onto the same measure-point codes the OAuth path produces (#269/#389). |
| `model_specs.py` / `model_capabilities.py` | Per-model datasheet metadata (`spec_for`, tracker/string counts, ratings) and coarse family resolution (`resolve_model_family`, `resolve_capabilities`, `mppt_points_for_model`) used to pick point ranges and gate battery controls. |

**Local Modbus**

| File | Responsibility |
| --- | --- |
| `modbus.py` | `SungrowModbusClient` — async Modbus TCP reads against the per-family register maps, family auto-detection from register 5000, block partitioning/skipping, and `modbus_diagnostics` bookkeeping. Model-gates MPPT points via `_points_for_model` (only trackers the model has; zero readings kept for those, so an idle tracker still yields an entity — #398), recording dropped codes for triage. |
| `modbus_registers.py` | The register maps themselves (`SG_RS_INPUT_POINTS`, `SH_RT_INPUT_POINTS`, `REGISTER_MAPS`), decoders, enum tables, `needs_derived_daily_yield()`, absent-meter suppression (`suppress_absent_meter_points`), and the opt-in `daily_yield` diagnostic dump (#223). |
| `modbus_control.py` / `modbus_control_probe.py` | Local dispatch over holding registers (`ModbusControl`, duck-typing `Control`) and the one-shot probe used to decide whether writes are supported. |
| `helpers.py` | Small shared helpers, e.g. `async_test_modbus_host()` TCP reachability used by the local wizard. |

**Entities**

| File | Responsibility |
| --- | --- |
| `sensor.py` | Builds `SungrowSensor` (plant), `SungrowDeviceSensor` (per-device) and `SungrowPlantDetailSensor` (plant-level health/tariffs from getPowerStationDetail, #178) entities from the stored coordinators. `infer_device_class()` maps units → device/state class so the Energy dashboard works; `_DIAGNOSTIC_CODES` marks the diagnostic points. |
| `binary_sensor.py` | Per-device `SungrowDeviceFaultBinarySensor` (PROBLEM, from `dev_fault_status`; exposes an `operating_status` reason attribute, #182) and `SungrowDeviceConnectivityBinarySensor` (CONNECTIVITY, from `dev_status`, exposes commissioning date). |
| `number.py` / `select.py` | Dispatch **Number**/**Select** entities (charge/discharge, SOC limits, forced charging, export/active-power limits, reactive power). `battery_only` params are gated on `coordinator.has_battery` (#148); power sliders are sized to the device's rated power. Write-only controls set `assumed_state` (no API read-back). `select.py` owns the EMS heartbeat lifecycle and the **auto-revert** timeout (`SungrowForcedDispatchDurationNumber`), and after a Charge/Discharge write **verifies actuation** by reading Energy Management Mode (10003) back — retrying the forced-mode write once, then raising the `dispatch_not_actuated` Repair if the inverter never left Self-consumption (#254, Confirm→Retry→Notify). |
| `schedule.py` | Daily-repeating forced charge/discharge windows (#359): evaluates the active window at startup, arms per-boundary callbacks, re-applies an enclosing window's mode when a nested one ends, and cancels in-flight boundary tasks on unload. |
| `services.py` | The three services — `backfill`, `set_battery_mode`, `refresh_tokens` — plus their schemas/target resolution. Registered once for every transport from `async_setup_entry`. |

**Config flow**

| File | Responsibility |
| --- | --- |
| `config_flow.py` | Thin entry point assembling the mixins from `_config_flow/` into `SungrowConfigFlow` / `SungrowOptionsFlow`. |
| `_config_flow/` | Per-transport package (#354): `_base.py` (shared instance state + `async_remove` lifecycle), `_helpers.py`, `cloud_oauth.py` (two-phase OAuth: hub first, then **authorization via reauth** with an auto callback wait and a manual code/URL fallback — creating the hub first registers the callback view, fixing the first-install 404), `cloud_user.py`, `modbus_only.py` (guided WiNet-S wizard), `plant_selection.py` (#358 multi-plant picker), `reconfigure.py`, `options.py` (`SungrowOptionsFlow`), `zeroconf.py` (WiNet-S discovery; a re-discovery only refreshes the host of a **discovery-managed** entry, never a user-set one — #402/#414). |

**Naming & classification**

| File | Responsibility |
| --- | --- |
| `measure_points.py` / `measure_points_data.py` | English naming (`resolve_name`, `CODE_ALIASES`), unit/code classification (`resolve_classification` — its `derived=` flag promotes a `modbus_derived` daily grid figure to ENERGY/TOTAL_INCREASING, #471, while the raw register keeps the #431 measurement treatment, `normalize_unit`) and enum resolution, grounded in the official iSolarCloud measure-point catalogs. `measure_points_data.py` holds the catalog rows, enum tables and aliases. |
| `const.py` | Domain, config keys (`CONF_TRANSPORT`, `CONF_DISCOVERY_MANAGED_HOST`, …), `GATEWAYS`, scan-interval defaults, and the per-device point maps (`INVERTER_DIAGNOSTIC_POINTS`, `BATTERY_DEVICE_POINTS`, `METER_DEVICE_POINTS`, `COMM_MODULE_POINTS`, operating-status points). |

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
# Environment (uv recommended; any Py3.14 venv works)
uv venv --python 3.14 .venv
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

- **Python 3.14** (Home Assistant requires >=3.14 since 2026.3; the test harness pins
  `homeassistant==2026.9.1`), ruff (line length 120) for lint + format.
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
- **Commits to `main` must be signed** (branch protection has "Require signed
  commits" on). Configure a signing key once (`git config --global user.signingkey
  <fpr>` and `commit.gpgsign true`) — an unsigned commit cannot be merged, and
  `--no-verify`/rewriting will not help.
- Releases are driven by **`release-please.yml`** (release-please keeps a
  `chore(main): release X.Y.Z` PR; merging it tags the release and attaches the HACS
  `sungrow.zip`). There is no `release-pr.yml`/`publish-release.yml`.
- `ci.yml` mints **dev pre-releases** on a component-touching PR (`vX.Y.Z-pr.<pr>.<run>`)
  and **RCs** on a push to `main` (`vX.Y.Z-rc.N`), both on a synthetic version-only
  commit — `main` is never mutated for them. The `dev-release-main` job only runs once
  the push CI run completes, so rapid successive merges can cancel it and skip an RC.

## User-facing docs

`docs/TROUBLESHOOTING.md` is the first stop for auth/setup/"unavailable" reports;
keep it current when changing the auth or setup flow. Also update, as relevant:
`docs/local-modbus.md` (local transport, register-map behaviour), `docs/SENSORS.md`
(point → entity mapping, device grouping), `README.md` (features/limitations) and
`custom_components/sungrow/quality_scale.yaml` (the honest quality-scale record —
note it must reflect reality, e.g. it *does* register services).
