# Implementation Plan: Transport Mode Selector

## Overview

This plan implements an explicit transport-mode selector for the Sungrow iSolarCloud config flow, allowing users to choose between Cloud Only, Cloud + Modbus, and Modbus Only connectivity during manual setup. This is a `feat!` breaking change that bumps the config-entry VERSION from 2 to 3.

The implementation follows the design's dependency order: constants first, then the reachability helper, migration, config flow restructure, options/reconfigure adaptation, translations, runtime branching, and finally property-based tests for the entry data shape.

## Tasks

- [ ] 1. Define transport-mode constants and smoke test
  - [-] 1.1 Add `TRANSPORT_CLOUD_ONLY` and `TRANSPORT_CLOUD_MODBUS` constants to `const.py`
    - Add `TRANSPORT_CLOUD_ONLY = "cloud_only"` alongside the existing `TRANSPORT_MODBUS_ONLY`
    - Add `TRANSPORT_CLOUD_MODBUS = "cloud_modbus"` alongside the existing `TRANSPORT_MODBUS_ONLY`
    - Ensure all three constants are exported and importable
    - _Requirements: 1.1, 1.2, 1.3_

  - [ ]* 1.2 Write smoke test for transport constants
    - Create `tests/test_transport_constants.py`
    - Assert `TRANSPORT_CLOUD_ONLY == "cloud_only"`, `TRANSPORT_CLOUD_MODBUS == "cloud_modbus"`, `TRANSPORT_MODBUS_ONLY == "modbus_only"`
    - Assert `SungrowConfigFlow.VERSION == 3` (will pass after task 3.1)
    - _Requirements: 1.1, 1.2, 1.3, 5.1_

- [ ] 2. Implement reachability helper and property tests
  - [~] 2.1 Create `helpers.py` with `async_test_modbus_host`
    - Create `custom_components/sungrow/helpers.py`
    - Implement `async def async_test_modbus_host(host: str, port: int = 502, timeout: float = 5.0) -> bool`
    - Use `asyncio.open_connection` with timeout; return `True` on success (close socket), `False` on any failure
    - Catch `OSError`, `asyncio.TimeoutError`, and any other exception — never raise to the caller
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

  - [ ]* 2.2 Write property test: Reachability test uses correct port and timeout
    - **Property 5: Reachability test uses correct port and timeout**
    - **Validates: Requirements 12.1, 12.2**
    - Create `tests/test_reachability_properties.py`
    - Use Hypothesis to generate arbitrary host strings; mock `asyncio.open_connection` and assert it is called with port 502 and a 5-second timeout
    - Minimum 100 iterations

  - [ ]* 2.3 Write property test: Reachability test returns bool without exceptions
    - **Property 6: Reachability test returns bool without exceptions**
    - **Validates: Requirements 12.3, 12.4**
    - In `tests/test_reachability_properties.py`
    - Use Hypothesis to generate arbitrary host strings; mock `asyncio.open_connection` to either succeed or raise various exceptions (TimeoutError, OSError, ConnectionRefusedError, gaierror)
    - Assert return value is always a `bool` and no exception propagates
    - Minimum 100 iterations

- [ ] 3. Config entry migration v2→v3 (⚠️ BREAKING: VERSION bump 2→3)
  - [~] 3.1 Extend `async_migrate_entry` in `__init__.py` and bump VERSION to 3
    - Add v2→v3 migration block after the existing v1→v2 block in `async_migrate_entry`
    - If `CONF_TRANSPORT` is absent, set it to `TRANSPORT_CLOUD_ONLY`; if already `TRANSPORT_MODBUS_ONLY`, leave unchanged
    - Call `hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)`
    - Update `SungrowConfigFlow` class: set `VERSION = 3`
    - Import `TRANSPORT_CLOUD_ONLY` in `__init__.py`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ]* 3.2 Write property test: v2→v3 migration correctly sets or preserves transport
    - **Property 2: v2→v3 migration correctly sets or preserves transport**
    - **Validates: Requirements 5.2, 5.3, 5.4**
    - Create `tests/test_migration_properties.py`
    - Use Hypothesis to generate version-2 entry data dicts (with/without `CONF_TRANSPORT`)
    - Assert: if absent → `"cloud_only"`; if `"modbus_only"` → preserved; version == 3
    - Minimum 100 iterations

  - [ ]* 3.3 Write property test: v1→v3 chained migration preserves semantics
    - **Property 3: v1→v3 chained migration preserves semantics**
    - **Validates: Requirements 5.5, 5.6**
    - In `tests/test_migration_properties.py`
    - Use Hypothesis to generate version-1 entries with positive integer `scan_interval` (1–1440 minutes)
    - Assert: after migration, version == 3, scan_interval == original × 60, transport is set
    - Minimum 100 iterations

  - [ ]* 3.4 Write unit tests for migration edge cases
    - Test v2 entry with no transport field → gets `cloud_only`, version 3
    - Test v2 entry with `modbus_only` → kept as `modbus_only`, version 3
    - Test v1 entry with scan_interval=5 → migrated to 300s, transport backfilled, version 3
    - Test already-v3 entry → no changes
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6_

- [~] 4. Checkpoint – Constants, helper, and migration
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Restructure config flow with transport selector
  - [~] 5.1 Implement `async_step_user` as transport selector step
    - Rename the existing `async_step_user` method to `async_step_cloud_credentials`
    - Create new `async_step_user` that presents a selector with three options: Cloud Only, Cloud + Modbus, Modbus Only
    - Use `vol.Schema` with a `selector.SelectSelector` for the transport choices
    - Route `cloud_only` → `async_step_cloud_credentials`, `cloud_modbus` → `async_step_cloud_credentials` (with flag), `modbus_only` → `async_step_local_setup`
    - Store selected transport mode on `self` for downstream steps
    - _Requirements: 2.1, 2.5_

  - [~] 5.2 Implement `async_step_cloud_credentials` (renamed from old user step)
    - Ensure the old `async_step_user` logic (app_key, app_secret, app_id, gateway, redirect_uri) now lives in `async_step_cloud_credentials`
    - After credential submission: if mode is `cloud_modbus` → proceed to `async_step_modbus_host`; else → proceed to OAuth / create tokenless entry
    - Store `TRANSPORT_CLOUD_ONLY` or `TRANSPORT_CLOUD_MODBUS` in entry data accordingly
    - _Requirements: 2.2, 2.3_

  - [~] 5.3 Implement `async_step_modbus_host` for hybrid mode
    - Show a text field for WiNet-S IP/hostname
    - Pre-fill from zeroconf discovery context if available
    - On submit: call `async_test_modbus_host(host)` from `helpers.py`
    - On success: store `CONF_MODBUS_HOST` in entry data, proceed to OAuth
    - On failure: show `host_unreachable` error, allow retry
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [~] 5.4 Implement `async_step_local_setup` for Modbus Only manual entry
    - Collect host, serial, and model fields
    - On submit: call `async_test_modbus_host(host)` for reachability
    - On success: create entry with `TRANSPORT_MODBUS_ONLY`, `CONF_MODBUS_HOST`, `CONF_SERIAL`, `CONF_MODEL`
    - On failure: show `host_unreachable` error, allow retry
    - Ensure zeroconf flow remains unchanged (does NOT hit transport step)
    - _Requirements: 2.4, 7.1, 7.2_

  - [ ]* 5.5 Write unit tests for config flow per transport mode
    - Test `async_step_user` shows transport selector with 3 options
    - Test cloud_only flow: user → cloud_credentials → creates entry with `transport=cloud_only`
    - Test cloud_modbus flow: user → cloud_credentials → modbus_host → creates entry with `transport=cloud_modbus` + `modbus_host`
    - Test modbus_only flow: user → local_setup → creates entry with `transport=modbus_only` + host/serial/model
    - Test zeroconf flow bypasses transport step
    - Test modbus_host step with unreachable host shows error
    - Test modbus_host step with zeroconf pre-fill
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.4, 7.2_

- [ ] 6. Adapt options flow for transport-mode switching
  - [~] 6.1 Modify `SungrowOptionsFlow.async_step_init` for transport-aware behaviour
    - For `cloud_only` entries: show existing options + optional `modbus_host` field
    - For `cloud_modbus` entries: show existing options + current `modbus_host` with clear option
    - For `modbus_only` entries: show existing options only, no transport-switching UI
    - _Requirements: 6.1, 6.4, 6.6_

  - [~] 6.2 Implement transport-mode switching logic in options flow
    - When `cloud_only` user provides modbus_host: run reachability test → on success update transport to `cloud_modbus`, add host, trigger reload
    - When `cloud_modbus` user clears modbus_host: update transport to `cloud_only`, remove host, trigger reload
    - On reachability failure: show error, keep current transport mode unchanged
    - _Requirements: 6.2, 6.3, 6.5, 6.7_

  - [ ]* 6.3 Write unit tests for options flow transport switching
    - Test cloud_only entry shows optional modbus_host field
    - Test providing valid host switches to cloud_modbus + triggers reload
    - Test unreachable host shows error, keeps cloud_only
    - Test cloud_modbus entry shows host + clear option
    - Test clearing host switches back to cloud_only + triggers reload
    - Test modbus_only entry does not show transport-switch options
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

- [ ] 7. Adapt reconfigure flow per transport mode
  - [~] 7.1 Modify reconfigure flow to branch on transport mode
    - For `cloud_only`: show cloud credentials form only (existing behaviour)
    - For `cloud_modbus`: show cloud credentials form followed by modbus host step with reachability test
    - For `modbus_only`: show modbus host form only (existing behaviour)
    - On host reachability failure in reconfigure: show error, allow correction
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ]* 7.2 Write unit tests for reconfigure flow adaptation
    - Test cloud_only reconfigure shows credentials form
    - Test cloud_modbus reconfigure shows credentials + modbus host step
    - Test cloud_modbus host update with reachability failure shows error
    - Test modbus_only reconfigure shows host form only
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [~] 8. Checkpoint – Config flow, options flow, reconfigure flow
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Add translation strings for all new UI elements
  - [~] 9.1 Update `strings.json` with transport step and modbus host keys
    - Add `config.step.user.title`, `config.step.user.description` for transport selector
    - Add `config.step.user.data.transport` label and selector option labels (Cloud Only, Cloud + Modbus, Modbus Only)
    - Add `config.step.modbus_host.title`, `config.step.modbus_host.description`, `config.step.modbus_host.data.modbus_host` label
    - Add `config.step.local_setup.title`, `config.step.local_setup.description`, field labels
    - Add `config.error.host_unreachable` error message
    - Add options flow keys for modbus_host field and transport-switch confirmation
    - _Requirements: 10.1, 10.2, 10.3_

  - [~] 9.2 Update `translations/en.json` to mirror all new keys from `strings.json`
    - Copy all new keys from `strings.json` into `translations/en.json` with English text
    - _Requirements: 10.4_

  - [~] 9.3 Propagate translation keys to all locale files (cy, de, es, fr)
    - Add the same key structure to `translations/cy.json`, `translations/de.json`, `translations/es.json`, `translations/fr.json`
    - Use English text as placeholder values (to be translated later by native speakers)
    - _Requirements: 10.4_

  - [ ]* 9.4 Write property test: Translation key parity
    - **Property 4: Translation key parity**
    - **Validates: Requirements 10.4**
    - Create `tests/test_translation_properties.py`
    - Load `strings.json` and `translations/en.json`, extract all key paths under `config` and `options` sections
    - Use Hypothesis to sample key paths and assert each exists in `translations/en.json` with a non-empty string value
    - Minimum 100 iterations

- [ ] 10. Implement runtime branching in `__init__.py`
  - [~] 10.1 Update `async_setup_entry` to branch on all three transport modes
    - Read `entry.data.get(CONF_TRANSPORT)` at entry setup
    - If `None` (missing): default to `cloud_only` and log a warning
    - If `modbus_only`: use existing `_async_setup_modbus_only` path (unchanged)
    - If `cloud_modbus`: set up cloud coordinator as normal, log that modbus_host is noted for future #217 wiring
    - If `cloud_only`: standard cloud coordinator setup (existing behaviour)
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [ ]* 10.2 Write integration tests for runtime branching
    - Test `async_setup_entry` with `cloud_only` → creates cloud coordinator (no modbus)
    - Test `async_setup_entry` with `cloud_modbus` → creates cloud coordinator, logs deferred message
    - Test `async_setup_entry` with `modbus_only` → calls `_async_setup_modbus_only`
    - Test `async_setup_entry` with missing transport → defaults to cloud_only, logs warning
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

- [ ] 11. Property test: Entry data shape per transport mode (Hypothesis)
  - [ ]* 11.1 Write property test for entry data shape validation
    - **Property 1: Entry data shape matches transport mode schema**
    - **Validates: Requirements 4.1, 4.2, 4.3**
    - Create `tests/test_entry_shape_properties.py`
    - Use Hypothesis strategies to generate entry data dicts for each transport mode
    - Assert: `cloud_only` has all cloud fields, no `modbus_host`; `cloud_modbus` has all cloud fields + `modbus_host`; `modbus_only` has `serial`, `model`, `modbus_host`, no cloud fields
    - Minimum 100 iterations

- [~] 12. Final checkpoint – Full test suite and linting
  - Run `pytest tests/` to ensure all tests pass
  - Run `ruff check custom_components/sungrow/` and `ruff format --check custom_components/sungrow/` to ensure no lint/format violations
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- ⚠️ Task 3.1 introduces the `feat!` breaking change (VERSION bump 2→3). The commit for this task MUST use `feat!:` prefix with footer `BREAKING CHANGE: Config entry VERSION bumped from 2 to 3; existing cloud entries are migrated to include an explicit transport field.` (Requirement 9)
- The actual hybrid data-path logic (Cloud + Modbus coordinator merging) is out of scope — deferred to issue #217
- Zeroconf flows are explicitly not touched (Requirement 7)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 4, "tasks": ["5.1", "9.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "9.2", "9.3"] },
    { "id": 6, "tasks": ["5.5", "6.1", "9.4"] },
    { "id": 7, "tasks": ["6.2", "7.1"] },
    { "id": 8, "tasks": ["6.3", "7.2", "10.1"] },
    { "id": 9, "tasks": ["10.2", "11.1"] }
  ]
}
```
