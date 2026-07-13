# Requirements Document

## Introduction

On initial setup, Home Assistant has no history for a Sungrow plant, so the History and
Energy dashboards start empty and only fill in as live polling accumulates data over days.
iSolarCloud exposes minute-level historical data through the plant-level endpoint
`getPowerStationPointMinuteDataList`, already wrapped by
`pysolarcloud`'s `Plants.async_get_historical_data`. This feature imports that historical
data into Home Assistant long-term statistics for yield/energy and power measure points,
so users see populated history and Energy-dashboard graphs immediately after setup.

The import runs once automatically after setup and can be re-run or extended on demand. It
must respect the iSolarCloud rate limit and quota, chunk requests to satisfy the endpoint's
per-call limits, produce statistics that line up with the live sensors, and never duplicate
or corrupt existing statistics when re-run. It must never block or degrade normal realtime
polling.

This feature covers historical import only. It is not a realtime polling change and does not
use the local Modbus transport.

## Glossary

- **Integration**: The Sungrow Home Assistant custom integration (`custom_components/sungrow`).
- **Backfill**: The process that retrieves historical measure-point data from iSolarCloud and
  imports it into Home Assistant long-term statistics.
- **Backfill_Engine**: The Integration component that orchestrates one Backfill run for one
  plant, including time/point chunking, ordering, throttling, and statistics import.
- **Plant_Coordinator**: An existing per-plant `SungrowPlantCoordinator` instance. A single
  config entry may own multiple Plant_Coordinators.
- **Historical_Data_API**: The `pysolarcloud` method
  `Plants.async_get_historical_data(plant_id, start_time, end_time, *, measure_points, interval)`,
  which wraps iSolarCloud's `getPowerStationPointMinuteDataList` endpoint.
- **Measure_Point**: A named cloud data channel (e.g. total yield, plant power) identified by
  a point code/id, as defined in `pysolarcloud` `Plants.measure_points`.
- **Backfill_Point**: A Measure_Point that the Backfill imports into statistics (yield/energy
  and power points).
- **Statistic_Id**: The Home Assistant long-term-statistics key for one series. For a live
  entity it is the entity's `entity_id`; for an external series it uses the `sungrow:` prefix.
- **Long_Term_Statistics**: Home Assistant's recorder statistics store, keyed by
  `(statistic_id, start_hour)`, holding hourly `sum`/`state` (for cumulative energy) and
  `mean`/`min`/`max` (for measurements).
- **Recorder_Statistics_API**: The Home Assistant recorder helpers
  `homeassistant.components.recorder.statistics.async_add_external_statistics` (external
  series with the `sungrow:` prefix) and `async_import_statistics` (real entity series).
- **History_Window**: The time range, ending at the current time and extending a bounded
  number of days into the past, that a Backfill run imports.
- **Point_Batch**: A group of at most 50 Backfill_Points sent in a single Historical_Data_API
  call, as required by the endpoint's per-call point limit.
- **Time_Chunk**: A sub-range of the History_Window sized to satisfy the endpoint's per-call
  query-window limit.
- **Rate_Limit_Error**: An iSolarCloud quota/throttle rejection with result code E998 (monthly)
  or E999 (hourly), as classified by the existing coordinator `RATE_LIMIT_ERRORS` set.
- **Backfill_Service**: The Home Assistant service the user invokes to run a Backfill on demand.
- **Repair_Issue**: A Home Assistant Repairs entry surfaced to the user for an actionable
  failure.

## Requirements

### Requirement 1: Automatic Backfill on initial setup

**User Story:** As a Sungrow user, I want history to be imported automatically when I first
set up the integration, so that my History and Energy dashboards are populated without any
manual steps.

#### Acceptance Criteria

1. WHEN a config entry completes its first successful setup and no prior Backfill has been
   recorded for a Plant_Coordinator, THE Backfill_Engine SHALL start one Backfill run for
   that Plant_Coordinator covering the default History_Window.
2. WHERE a config entry uses the local Modbus-only transport, THE Backfill_Engine SHALL NOT
   start a Backfill run for that config entry.
3. WHEN a Backfill run completes, THE Backfill_Engine SHALL persist a record of the run's
   completion and the covered History_Window for the Plant_Coordinator.
4. WHILE a Plant_Coordinator has a persisted completed Backfill record covering the default
   History_Window, THE Backfill_Engine SHALL NOT start an automatic Backfill run for that
   Plant_Coordinator on subsequent setups.
5. WHEN a config entry is reloaded while a Backfill run for one of its Plant_Coordinators is
   in progress, THE Backfill_Engine SHALL allow the in-progress run to be cancelled without
   corrupting previously imported statistics.

### Requirement 2: On-demand Backfill

**User Story:** As a Sungrow user, I want to re-run or extend the historical import on demand,
so that I can fill gaps or import a longer range after setup.

#### Acceptance Criteria

1. THE Integration SHALL register a Backfill_Service that triggers a Backfill run.
2. WHEN the Backfill_Service is invoked with a target config entry or plant, THE
   Backfill_Engine SHALL start a Backfill run for each addressed Plant_Coordinator.
3. WHERE the Backfill_Service is invoked with an explicit start date, THE Backfill_Engine SHALL
   use that start date as the beginning of the History_Window instead of the default.
4. IF the Backfill_Service is invoked for a Plant_Coordinator that already has a Backfill run
   in progress, THEN THE Backfill_Engine SHALL reject the new invocation for that
   Plant_Coordinator and report that a run is already in progress.
5. WHEN an on-demand Backfill run completes, THE Backfill_Engine SHALL update the persisted
   Backfill record for the Plant_Coordinator with the covered History_Window.

### Requirement 3: Bounded, configurable History Window

**User Story:** As a Sungrow user, I want control over how far back history is imported, so
that I get useful history without unbounded API usage.

#### Acceptance Criteria

1. THE Backfill_Engine SHALL use a default History_Window that extends a fixed default number
   of days into the past from the current time.
2. WHERE the user configures a History_Window length in the integration options, THE
   Backfill_Engine SHALL use the configured length instead of the default.
3. IF a requested History_Window length exceeds the maximum supported length, THEN THE
   Backfill_Engine SHALL clamp the History_Window to the maximum supported length and log the
   clamping.
4. THE Backfill_Engine SHALL bound every Backfill run to a finite History_Window so that a run
   cannot request time ranges indefinitely.
5. IF a requested start date is later than the current time, THEN THE Backfill_Engine SHALL
   reject the run and report an invalid range.

### Requirement 4: Chunking by points and by time

**User Story:** As a maintainer, I want requests chunked to satisfy the endpoint's limits, so
that Backfill calls succeed instead of being rejected.

#### Acceptance Criteria

1. THE Backfill_Engine SHALL divide the Backfill_Points into Point_Batches of at most 50
   points per Historical_Data_API call.
2. THE Backfill_Engine SHALL divide the History_Window into Time_Chunks that each fall within
   the endpoint's maximum per-call query window.
3. WHEN issuing a Historical_Data_API call, THE Backfill_Engine SHALL pass an explicit
   `start_time` and `end_time` bounding one Time_Chunk.
4. WHEN a Backfill run covers multiple Time_Chunks for a cumulative energy Backfill_Point, THE
   Backfill_Engine SHALL process and import those Time_Chunks in ascending chronological order.
5. THE Backfill_Engine SHALL import the cumulative energy statistics for each Backfill_Point so
   that the imported hourly `sum` values are non-decreasing across the History_Window.

### Requirement 5: Rate-limit and quota safety

**User Story:** As a Sungrow user on the free plan, I want Backfill to respect the API quota,
so that it does not exhaust my calls or break live updates.

#### Acceptance Criteria

1. THE Backfill_Engine SHALL throttle Historical_Data_API calls so that the configured rate
   limit is not exceeded across a Backfill run.
2. WHEN a Historical_Data_API call raises a Rate_Limit_Error, THE Backfill_Engine SHALL pause
   further calls and back off before retrying.
3. WHEN a Rate_Limit_Error back-off expires, THE Backfill_Engine SHALL resume the Backfill run
   from the first Time_Chunk and Point_Batch not yet successfully imported.
4. THE Backfill_Engine SHALL run without blocking or delaying the Plant_Coordinator's realtime
   poll cycle.
5. IF a Backfill run is in progress WHEN a realtime poll is due, THEN THE Plant_Coordinator
   SHALL perform its realtime poll on its normal schedule.
6. WHEN a transient (non-authentication, non-Rate_Limit) error occurs on a Historical_Data_API
   call, THE Backfill_Engine SHALL retry that call up to a bounded number of attempts before
   marking the affected chunk as failed.

### Requirement 6: Idempotent statistics import

**User Story:** As a Sungrow user, I want re-running Backfill to be safe, so that repeated
imports never duplicate or corrupt my history.

#### Acceptance Criteria

1. WHEN the Backfill_Engine imports statistics for a `(Statistic_Id, start_hour)` that already
   exists, THE Backfill_Engine SHALL overwrite the existing hourly value rather than create a
   duplicate.
2. WHEN a Backfill run is executed twice over the same History_Window with the same source
   data, THE Long_Term_Statistics for each affected Statistic_Id SHALL be identical after the
   second run as after the first run.
3. THE Backfill_Engine SHALL aggregate minute-level source data into whole-hour statistics
   aligned to `(Statistic_Id, start_hour)` before import.
4. IF a Time_Chunk fails and is later retried, THEN THE Backfill_Engine SHALL re-import that
   chunk's hours without altering statistics outside that chunk's hours.

### Requirement 7: Selection and identification of backfilled series

**User Story:** As a Sungrow user, I want the imported history to line up with my live
sensors, so that the same graph shows both historical and live data continuously.

#### Acceptance Criteria

1. THE Backfill_Engine SHALL import statistics for cumulative yield energy Backfill_Points in
   kilowatt-hours as `TOTAL` (cumulative) statistics.
2. THE Backfill_Engine SHALL import statistics for power Backfill_Points as measurement
   statistics with the power unit reported for the corresponding live sensor.
3. WHERE a live sensor exists for a Backfill_Point, THE Backfill_Engine SHALL import the
   statistics under a Statistic_Id that matches that live sensor's long-term-statistics
   series so historical and live data share one series.
4. WHERE no live sensor exists for a Backfill_Point, THE Backfill_Engine SHALL import the
   statistics as an external series using a `sungrow:` prefixed Statistic_Id.
5. THE Backfill_Engine SHALL set each imported series' unit to match the unit of the
   corresponding live sensor's statistics.
6. IF a Backfill_Point's source unit differs from the target statistics unit, THEN THE
   Backfill_Engine SHALL convert the source values to the target unit before import.

### Requirement 8: Failure handling and user feedback

**User Story:** As a Sungrow user, I want to understand when Backfill partially fails or finds
no data, so that I know whether my history is complete and what to do.

#### Acceptance Criteria

1. WHEN a Backfill run finishes with one or more failed Time_Chunks, THE Backfill_Engine SHALL
   import the successfully retrieved statistics and record the run as partially completed.
2. WHEN a Backfill run finishes with failed chunks, THE Backfill_Engine SHALL surface a
   Repair_Issue describing the partial failure and how to re-run the Backfill.
3. WHEN a Historical_Data_API call for a Plant_Coordinator returns no data rows for the
   requested range, THE Backfill_Engine SHALL log that no data was available and complete the
   run without importing statistics for the empty range.
4. WHEN a Backfill run starts, progresses, and completes, THE Backfill_Engine SHALL log the
   run's start, per-chunk progress, and final outcome.
5. IF a Historical_Data_API call fails with an authentication error, THEN THE Backfill_Engine
   SHALL stop the Backfill run and defer to the Integration's existing re-authentication
   handling.
6. WHEN a Backfill run ends, THE Backfill_Engine SHALL report a summary of imported hours,
   skipped empty ranges, and failed chunks.

### Requirement 9: Multi-plant support

**User Story:** As a Sungrow user with more than one plant on a config entry, I want each
plant's history imported, so that all my plants have populated graphs.

#### Acceptance Criteria

1. WHEN an automatic or on-demand Backfill is triggered for a config entry with multiple
   Plant_Coordinators, THE Backfill_Engine SHALL run a Backfill for each Plant_Coordinator.
2. THE Backfill_Engine SHALL import each Plant_Coordinator's statistics under Statistic_Ids
   scoped to that plant so that plants' series do not collide.
3. IF one Plant_Coordinator's Backfill run fails, THEN THE Backfill_Engine SHALL continue
   Backfill runs for the remaining Plant_Coordinators.
4. WHILE multiple Plant_Coordinators are being backfilled, THE Backfill_Engine SHALL apply the
   rate-limit throttle across all concurrent runs of the config entry so the combined call
   rate does not exceed the limit.
