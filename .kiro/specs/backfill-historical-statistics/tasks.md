# Implementation Plan: Backfill Historical Statistics

## Overview

This plan implements the Backfill feature for the Sungrow iSolarCloud integration
(`custom_components/sungrow`). It follows the design's separation of a **pure-logic core**
(window resolution, chunking, batching, hourly aggregation, unit conversion, statistic-id
scoping) that is exercised by **Hypothesis property tests**, from the **I/O orchestration shell**
(series resolution, recorder import, throttle, engine run loop, manager, wiring, service, and
Repairs) that is exercised by mocked `pytest` + `pytest-asyncio` unit/integration tests.

Tasks are ordered by dependency: constants and data models first, then the pure functions with
their property tests, then the orchestration shell wired into `__init__.py`, the on-demand
service, and failure feedback. Property tests use `@settings(max_examples=100)` (minimum 100
examples) and reference their design property and requirement IDs. Test sub-tasks are marked
optional with `*`.

Target language: **Python** (Home Assistant custom integration). Test stack: `pytest`,
`pytest-asyncio`, and **Hypothesis** (add to `requirements_test.txt` if not already present).

## Tasks

- [ ] 1. Add Backfill constants and options to `const.py`
  - Add `CONF_BACKFILL_DAYS`, `DEFAULT_BACKFILL_DAYS = 30`, `MAX_BACKFILL_DAYS = 365`
  - Add `BACKFILL_INTERVAL = timedelta(minutes=5)`, `BACKFILL_CHUNK_WINDOW = timedelta(hours=3)`
  - Add `MAX_POINTS_PER_CALL = 50`, `BACKFILL_MIN_CALL_INTERVAL = 1.0`, `BACKFILL_MAX_RETRIES = 3`
  - Ensure `timedelta` is imported in `const.py`
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 5.1, 5.6_

- [ ] 2. Define core data models and persistence in `backfill.py`
  - [ ] 2.1 Create `backfill.py` with frozen dataclasses and error type
    - Define `MinuteRow` (`timestamp: datetime`, `value: float`), `HistoryWindow`
      (`start`, `end`, both UTC), `SeriesTarget` (`point_code`, `statistic_id`, `unit`,
      `kind`, `is_external`, `metadata`), and `RunSummary` (`plant_id`, `window`,
      `imported_hours`, `skipped_empty_ranges`, `failed_chunks`, `completed`)
    - Define `InvalidRangeError(Exception)`
    - _Requirements: 3.4, 4.1, 7.1, 7.2, 8.6_
  - [ ] 2.2 Implement `BackfillStore` marker persistence wrapper
    - Wrap HA `Store[dict]` at key `f"{DOMAIN}.backfill_state_{entry.entry_id}"`, version 1
    - Provide async read/write of per-plant markers (`completed`, `partial`, `window_start`,
      `window_end`, `last_run`, `failed_chunks`)
    - _Requirements: 1.3, 1.4, 2.5_

- [ ] 3. Implement pure window resolution
  - [ ] 3.1 Implement `resolve_window(*, now, option_days, start_override)` in `backfill.py`
    - Default length `DEFAULT_BACKFILL_DAYS`; use `option_days` when provided; clamp to
      `[1 day, MAX_BACKFILL_DAYS]` and log when clamped; `end` always equals `now`
    - Honor `start_override` (subject to clamp); raise `InvalidRangeError` when
      `start_override > now`
    - _Requirements: 2.3, 3.1, 3.2, 3.3, 3.4, 3.5_
  - [ ]* 3.2 Write property test for window resolution in `test_backfill_properties.py`
    - **Property 1: Window resolution is bounded and honors configuration**
    - **Validates: Requirements 2.3, 3.1, 3.2, 3.3, 3.4, 3.5**
    - Generate `now`, `option_days` (including > MAX and ≤ 0), and optional `start_date`
      (before and after `now`); assert finiteness, `end == now`, clamped length, honored
      override, and `InvalidRangeError` for future start dates; `@settings(max_examples=100)`

- [ ] 4. Implement time chunking and point batching
  - [ ] 4.1 Implement `chunk_time_window(window, chunk)` in `backfill.py`
    - Split `[start, end)` into consecutive, non-overlapping sub-ranges each ≤ `chunk`,
      returned ascending by start
    - _Requirements: 4.2, 4.3, 4.4_
  - [ ]* 4.2 Write property test for time chunking in `test_backfill_properties.py`
    - **Property 3: Time chunking covers the window in ascending, bounded, non-overlapping pieces**
    - **Validates: Requirements 4.2, 4.3, 4.4**
    - Generate windows and positive chunk sizes; assert ascending, contiguous, non-overlapping,
      full coverage of `[start, end)`, and per-chunk duration bound; `@settings(max_examples=100)`
  - [ ] 4.3 Implement `batch_points(points, max_size=50)` in `backfill.py`
    - Partition points into batches of at most `max_size` preserving order
    - _Requirements: 4.1_
  - [ ]* 4.4 Write property test for point batching in `test_backfill_properties.py`
    - **Property 2: Point batching preserves all points and never exceeds the cap**
    - **Validates: Requirements 4.1**
    - Generate random point lists and `max_size ∈ [1, 50]`; assert batch-size cap and
      order-preserving concatenation equals input; `@settings(max_examples=100)`

- [ ] 5. Implement statistics builder and unit conversion
  - [ ] 5.1 Implement `build_hourly_statistics(rows, kind, *, running_sum=0.0)` in `backfill.py`
    - Floor each row timestamp to the UTC hour; energy: per-hour `state` = last cumulative
      value in the hour, `sum` = running total, clamp negative minute deltas to zero so `sum`
      is non-decreasing; power: per-hour `mean`/`min`/`max`; return
      `(list[StatisticData], updated_running_sum)`
    - _Requirements: 4.5, 6.3, 7.1, 7.2_
  - [ ]* 5.2 Write property test for non-decreasing energy sum in `test_backfill_properties.py`
    - **Property 4: Cumulative energy sum is non-decreasing across the whole window**
    - **Validates: Requirements 4.5, 7.1**
    - Generate minute rows (monotonic-ish readings plus noise, gaps, resets) split across
      chunks with `running_sum` carried forward; assert the concatenated hourly `sum` sequence
      is non-decreasing; `@settings(max_examples=100)`
  - [ ]* 5.3 Write property test for determinism + idempotent, hour-local import in `test_backfill_properties.py`
    - **Property 5: Aggregation is deterministic and import is idempotent and hour-local**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    - Assert `build_hourly_statistics` is deterministic, one `StatisticData` per hour, every
      `start` on an exact UTC hour; using a fake `(statistic_id, start_hour)`-keyed store that
      overwrites on collision, assert identical stored map for all-at-once vs per-chunk and
      one run vs two runs, and that re-importing one chunk changes only that chunk's hours;
      `@settings(max_examples=100)`
  - [ ] 5.4 Implement energy unit conversion reuse in `backfill.py`
    - Reuse `energy_units.normalize_energy_point` on each energy minute row (Wh→kWh) before
      aggregation; leave power (W) and already-correct units unchanged
    - _Requirements: 7.5, 7.6_
  - [ ]* 5.5 Write property test for unit conversion in `test_backfill_properties.py`
    - **Property 6: Energy unit conversion is correct and stable**
    - **Validates: Requirements 7.5, 7.6**
    - Generate numeric values and units; assert Wh→kWh yields `round(value / 1000, 3)` with
      unit `kWh`, and no-op for kWh/other units; `@settings(max_examples=100)`

- [ ] 6. Checkpoint - Ensure all pure-core tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement series resolution and statistic-id scoping
  - [ ] 7.1 Implement `async_resolve_series` and statistic-id derivation in `backfill.py`
    - Select cumulative-energy and power Backfill_Points from
      `coordinator.plants_service.measure_points` via `resolve_classification`
    - Look up live entity by `unique_id` `f"{plant_id}_{point_code}"`; live entity →
      `statistic_id = entity_id`, `source="recorder"`; else external →
      `statistic_id = f"sungrow:{plant_id}_{point_code}"`, `source=DOMAIN`
    - Derive `has_mean`/`has_sum` from kind (energy → `has_sum`; power → `has_mean`) and set
      unit from the live entity/statistics unit
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 9.2_
  - [ ]* 7.2 Write property test for statistic-id scoping in `test_backfill_properties.py`
    - **Property 7: Statistic_Ids are scoped per plant and never collide**
    - **Validates: Requirements 9.2**
    - Generate distinct `plant_id` pairs and codes; assert external ids
      `f"sungrow:{plant_id}_{code}"` differ; `@settings(max_examples=100)`
  - [ ]* 7.3 Write unit tests for series resolution in `test_backfill.py`
    - Live-entity resolution via a populated entity registry (7.3, 7.5); external fallback when
      absent (7.4); energy vs power metadata shape and unit propagation (7.1, 7.2)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

- [ ] 8. Implement idempotent import router
  - [ ] 8.1 Implement `import_statistics(hass, target, data)` in `backfill.py`
    - Route external series to `async_add_external_statistics` and live-entity series to
      `async_import_statistics`; both overwrite by `(statistic_id, start_hour)`
    - _Requirements: 6.1, 6.4, 7.3, 7.4_
  - [ ]* 8.2 Write unit tests for import idempotency in `test_backfill.py`
    - Use a fake `(statistic_id, start_hour)`-keyed recorder store; assert re-import overwrites
      rather than duplicates and that a retried chunk touches only its own hours
    - _Requirements: 6.1, 6.2, 6.4_

- [ ] 9. Implement shared Throttle
  - [ ] 9.1 Implement `Throttle` class in `backfill.py`
    - `acquire()` waits until `min_interval` has elapsed since the previous acquire
      (serialised across engines sharing one instance); `backoff()` sleeps an escalating delay
      (doubling, capped at 1 hour); `reset_backoff()` clears it
    - _Requirements: 5.1, 5.2, 5.3, 9.4_
  - [ ]* 9.2 Write unit tests for throttle in `test_backfill.py`
    - Mocked-clock tests for min-interval spacing across shared instance (5.1, 9.4) and
      escalating backoff with cap (5.2)
    - _Requirements: 5.1, 5.2, 9.4_

- [ ] 10. Implement error-classification wiring
  - [ ] 10.1 Wire error classifiers into `backfill.py`
    - Reuse the coordinator's `is_auth_error` and `is_rate_limit_error` (E998/E999) helpers to
      classify `async_get_historical_data` failures
    - _Requirements: 5.2, 5.6, 8.5_

- [ ] 11. Implement `BackfillEngine.async_run`
  - [ ] 11.1 Implement the engine run loop in `backfill.py`
    - Resolve window and series; iterate Time_Chunks ascending × Point_Batches (≤ 50); await
      throttle before each call; pass explicit `start_time`/`end_time` and
      `interval=BACKFILL_INTERVAL`; aggregate via `build_hourly_statistics` carrying
      `running_sum`; import via `import_statistics`; classify errors (auth → stop/defer;
      rate-limit → backoff + resume from progress cursor; transient → retry ≤
      `BACKFILL_MAX_RETRIES` then mark chunk failed); handle empty ranges; persist marker; log
      start/per-chunk/outcome; return `RunSummary`
    - _Requirements: 1.3, 4.3, 4.4, 5.2, 5.3, 5.6, 6.4, 8.1, 8.3, 8.4, 8.6_
  - [ ]* 11.2 Write unit tests for the happy-path run loop in `test_backfill.py`
    - Mocked `plants_service.async_get_historical_data`; assert ascending chunk loop, explicit
      call kwargs bounding a chunk (4.3), empty-range skip + log (8.3), start/progress/outcome
      logs via `caplog` (8.4), and `RunSummary` counts (8.6)
    - _Requirements: 4.3, 8.3, 8.4, 8.6_
  - [ ]* 11.3 Write unit tests for run-loop error handling in `test_backfill.py`
    - Rate-limit backoff + resume from cursor (5.2, 5.3); bounded transient retries then
      chunk-failed and run continues (5.6, 8.1); auth-error stop/defer (8.5)
    - _Requirements: 5.2, 5.3, 5.6, 8.1, 8.5_

- [ ] 12. Checkpoint - Ensure engine and pure-core tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement `BackfillManager`
  - [ ] 13.1 Implement the manager in `backfill.py`
    - One `BackfillEngine` per coordinator sharing one `Throttle` and one `BackfillStore`;
      `async_start_automatic` gates each plant on the completed default-window marker;
      `async_run_on_demand(plant_ids, start_date)` starts runs for addressed coordinators and
      rejects a plant already running; `is_running(plant_id)`; `async_shutdown` cancels and
      awaits in-flight tasks; wrap per-plant runs so one failure does not stop the others; log
      each `RunSummary`
    - _Requirements: 1.1, 1.4, 1.5, 2.2, 2.3, 2.4, 9.1, 9.3, 9.4_
  - [ ]* 13.2 Write unit tests for the manager in `test_backfill.py`
    - Automatic-start gating on marker (1.1, 1.4); already-running rejection (2.4); multi-plant
      run and one plant failing without stopping others (9.1, 9.3); shared throttle across
      engines (9.4); shutdown cancels tasks leaving imports intact (1.5)
    - _Requirements: 1.1, 1.4, 1.5, 2.4, 9.1, 9.3, 9.4_

- [ ] 14. Wire Backfill into integration setup (`__init__.py`)
  - [ ] 14.1 Add `backfill` field to `SungrowData` and wire lifecycle
    - Add `backfill: "BackfillManager | None" = None` to `SungrowData`; in the cloud
      `async_setup_entry` path construct the manager and schedule
      `manager.async_start_automatic()` via `entry.async_create_background_task` (gated on
      recorder readiness); skip construction in the Modbus-only setup path; call
      `manager.async_shutdown()` in `async_unload_entry`
    - _Requirements: 1.1, 1.2, 1.5, 5.4, 5.5_
  - [ ]* 14.2 Write unit tests for setup wiring in `test_backfill.py`
    - Cloud setup constructs manager and starts automatic run; Modbus-only skip (1.2);
      realtime poll not blocked while a run task is parked on the throttle (5.4, 5.5); unload
      cancellation leaves imports intact (1.5)
    - _Requirements: 1.2, 1.5, 5.4, 5.5_

- [ ] 15. Implement the on-demand `BackfillService`
  - [ ] 15.1 Create `services.yaml` and register the admin service
    - Add `services.yaml` `backfill` entry with `config_entry` and `start_date` selectors;
      register `sungrow.backfill` as an admin service; handler resolves addressed config
      entries/plants and calls `manager.async_run_on_demand(plant_ids=..., start_date=...)`
    - _Requirements: 2.1, 2.2, 2.3_
  - [ ]* 15.2 Write unit tests for the service in `test_backfill.py`
    - Service registration smoke test (2.1); dispatch to multiple plants (2.2); explicit
      `start_date` passed through to the manager (2.3)
    - _Requirements: 2.1, 2.2, 2.3_

- [ ] 16. Implement failure feedback (Repairs + translations)
  - [ ] 16.1 Add partial-failure Repair and translations
    - On a partial run, `ir.async_create_issue` with translation key `backfill_partial`
      describing the failure and how to re-run; clear the issue on a later fully successful run;
      add the `backfill_partial` strings to `strings.json` and `translations/en.json`
    - _Requirements: 8.2_
  - [ ]* 16.2 Write unit tests for Repair behaviour in `test_backfill.py`
    - Assert a partial run raises the `backfill_partial` issue (8.1, 8.2) and a subsequent
      successful run clears it
    - _Requirements: 8.1, 8.2_

- [ ] 17. Final checkpoint - Ensure the full suite and lint pass
  - Ensure all tests pass and `ruff` is clean, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP.
- Each task references specific requirement sub-clauses for traceability.
- Property tests (Properties 1–7) own the combinatorial input space and run a minimum of 100
  Hypothesis examples; unit tests own wiring, error branches, HA registry/recorder integration,
  and logging as representative examples.
- Property tests live in `tests/test_backfill_properties.py`; unit/integration tests live in
  `tests/test_backfill.py`. Add Hypothesis to `requirements_test.txt` if absent.
- Checkpoints ensure incremental validation of the pure core, the engine, and the full feature.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["3.1"] },
    { "id": 4, "tasks": ["3.2", "4.1"] },
    { "id": 5, "tasks": ["4.2", "4.3"] },
    { "id": 6, "tasks": ["4.4", "5.1"] },
    { "id": 7, "tasks": ["5.2", "5.4"] },
    { "id": 8, "tasks": ["5.3", "7.1"] },
    { "id": 9, "tasks": ["5.5", "8.1"] },
    { "id": 10, "tasks": ["7.2", "9.1"] },
    { "id": 11, "tasks": ["7.3", "10.1"] },
    { "id": 12, "tasks": ["8.2", "11.1"] },
    { "id": 13, "tasks": ["9.2", "13.1"] },
    { "id": 14, "tasks": ["11.2", "14.1"] },
    { "id": 15, "tasks": ["11.3", "15.1"] },
    { "id": 16, "tasks": ["13.2", "16.1"] },
    { "id": 17, "tasks": ["14.2"] },
    { "id": 18, "tasks": ["15.2"] },
    { "id": 19, "tasks": ["16.2"] }
  ]
}
```
