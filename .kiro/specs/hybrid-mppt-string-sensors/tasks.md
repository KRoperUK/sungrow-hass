# Implementation Plan

## Overview

Fix SH-family hybrid (ESS) inverters not surfacing MPPT voltage/current diagnostic sensors.
The coordinator's ESS branch swaps the string-inverter MPPT IDs (`"5"`-`"10"`) for the hybrid
13xxx MPPT IDs (preserving the `"29"` operating-status drop), a new `ESS_MPPT_DIAGNOSTIC_POINTS`
map is added to `const.py`, and its values are unioned into `sensor.py`'s `_DIAGNOSTIC_CODES`.
Tasks follow the exploratory bugfix methodology: exploration test (fails on unfixed code) and
preservation tests (pass on unfixed code) come before the fix, followed by Fix Checking and
Preservation Checking.

## Tasks

- [ ] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Hybrid MPPT Sensors Surfaced and Classified Diagnostic
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails** - the failure is the goal of this task
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists (root cause #1: the 13xxx MPPT IDs are never requested for ESS)
  - **Scoped PBT Approach**: The bug is deterministic per device type/config, so scope the property to the concrete failing configuration: `DeviceType.ENERGY_STORAGE_SYSTEM` + `CONF_ENABLE_DEVICE_SENSORS: True`. Assert the universal invariant across the fixed set of hybrid MPPT IDs.
  - Add to `tests/test_coordinator.py`, mirroring `test_ess_operating_status_avoids_point29_collision`: build a `SungrowPlantCoordinator` with a single `DeviceType.ENERGY_STORAGE_SYSTEM` device and `CONF_ENABLE_DEVICE_SENSORS: True`, mock `plants.async_get_device_realtime` (AsyncMock) and `plants.async_get_realtime_data`, run `await coordinator._async_update_data()`, and capture `plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]`
  - Assertion A (from Bug Condition / isBugCondition in design): `{"13001","13002","13105","13106","13107","13108","13109","13110"}` is a subset of the captured `extra` (FAILS on unfixed code - none are present)
  - Assertion B (collision source, from design Property 2 final clause): for the ESS `extra`, the string-inverter MPPT IDs `{"5","6","7","8","9","10"}` are absent (FAILS on unfixed code - they are present)
  - Assertion C (diagnostic classification, from expectedBehavior): add to `tests/test_sensor.py` that `mppt4_voltage` and `mppt4_current` are members of `sensor._DIAGNOSTIC_CODES` (FAILS on unfixed code - only mppt1-3 present)
  - Run the tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bug exists)
  - Document counterexamples found: ESS `extra_measure_points` contains no 13xxx MPPT IDs and still contains `"5"`-`"10"`; `mppt4_voltage`/`mppt4_current` not in `_DIAGNOSTIC_CODES`
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.1, 1.2, 2.1, 2.2_

- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-Buggy Device/Config Behavior Unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record actual outputs, then assert those observed outputs
  - **GOAL**: Capture the baseline behavior of every non-buggy path so the fix can be proven to leave it byte-for-byte identical
  - Observe on UNFIXED code and record: for `DeviceType.INVERTER` + sensors on, `extra` contains `"5"`-`"10"` and per-string IDs (`"96"`/`"70"` .. `"103"`/`"77"`); for ESS, `extra["13146"] == "operating_status"` and `"29"` absent; for sensors off, inverter requests only `{"29": ...}` and ESS only `{"13146": ...}` with no diagnostic set; for BATTERY/METER/COMMUNICATION_MODULE, each requests its existing point set; and all `INVERTER_DIAGNOSTIC_POINTS.values()` are in `_DIAGNOSTIC_CODES`
  - Write property-based tests (mirroring `test_ess_operating_status_avoids_point29_collision` in `tests/test_coordinator.py`) that generate device type ∈ {INVERTER, BATTERY, METER, COMMUNICATION_MODULE, unmapped} × `enable_device_sensors` ∈ {true, false}, and for each non-buggy combination assert the captured `extra_measure_points` equals the observed golden set
  - Add a preservation assertion in `tests/test_sensor.py` that all existing `INVERTER_DIAGNOSTIC_POINTS.values()` remain members of `_DIAGNOSTIC_CODES` (Requirement 3.5)
  - Run the tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 3. Fix for hybrid (ESS) MPPT diagnostic sensors not being surfaced

  - [ ] 3.1 Add `ESS_MPPT_DIAGNOSTIC_POINTS` to const.py
    - In `custom_components/sungrow/const.py`, add `ESS_MPPT_DIAGNOSTIC_POINTS: dict[str, str]` mapping the hybrid 13xxx MPPT IDs to the reused string-inverter code names: `"13001"→mppt1_voltage`, `"13002"→mppt1_current`, `"13105"→mppt2_voltage`, `"13106"→mppt2_current`, `"13107"→mppt3_voltage`, `"13108"→mppt3_current`, `"13109"→mppt4_voltage`, `"13110"→mppt4_current`
    - Add the explanatory comment noting these IDs already exist in `measure_points_data.py` (units V/A) and classify by unit automatically
    - _Bug_Condition: isBugCondition(input) where deviceType = ENERGY_STORAGE_SYSTEM AND enableDeviceSensors AND modelReportsHybridMppt_
    - _Expected_Behavior: expectedBehavior(result) - hybrid MPPT IDs available to be requested and produced codes reuse mpptN_* names_
    - _Preservation: no existing const changed; new map is additive_
    - _Requirements: 2.1_

  - [ ] 3.2 Swap string-inverter MPPT IDs for the 13xxx IDs in the coordinator ESS branch
    - In `custom_components/sungrow/coordinator.py`, import `ESS_MPPT_DIAGNOSTIC_POINTS` alongside `INVERTER_DIAGNOSTIC_POINTS`
    - In the `ENERGY_STORAGE_SYSTEM` diagnostic branch (~line 538), rework the existing `"29"`-drop comprehension so that for ESS it ALSO removes the string-inverter MPPT IDs `{"5","6","7","8","9","10"}`, then merge `ESS_MPPT_DIAGNOSTIC_POINTS` in (`diagnostic = {**diagnostic, **ESS_MPPT_DIAGNOSTIC_POINTS}`)
    - Preserve the `"29"` drop exactly so the `13146`/`29` operating_status collision handling (#182) is unchanged
    - Leave the `INVERTER` branch, battery/meter/comm branches, and the sensors-off path untouched
    - _Bug_Condition: isBugCondition(input) - ESS + sensors on + reports hybrid MPPT_
    - _Expected_Behavior: expectedBehavior(result) - requested extra contains the eight 13xxx IDs and none of "5"-"10"; requesting both would collide on mpptN_* codes_
    - _Preservation: "29" dropped, "13146" requested; INVERTER and other device branches unchanged_
    - _Requirements: 2.1, 2.3, 3.1, 3.2_

  - [ ] 3.3 Union `ESS_MPPT_DIAGNOSTIC_POINTS.values()` into `_DIAGNOSTIC_CODES` in sensor.py
    - In `custom_components/sungrow/sensor.py`, import `ESS_MPPT_DIAGNOSTIC_POINTS` from `.const`
    - Add `| frozenset(ESS_MPPT_DIAGNOSTIC_POINTS.values())` to the `_DIAGNOSTIC_CODES` union (idempotent for mppt1-3; adds the new `mppt4_voltage`/`mppt4_current`)
    - _Bug_Condition: isBugCondition(input) - produced hybrid MPPT codes must be diagnostic_
    - _Expected_Behavior: expectedBehavior(result) - every produced hybrid MPPT code ∈ _DIAGNOSTIC_CODES so sensors land in the Diagnostic section_
    - _Preservation: union is additive; all existing INVERTER/battery/comm codes remain members_
    - _Requirements: 2.2, 3.5_

  - [ ] 3.4 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Hybrid MPPT Sensors Surfaced and Classified Diagnostic
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm the fix satisfies it
    - Run the bug condition exploration tests from task 1
    - **EXPECTED OUTCOME**: Tests PASS (confirms the bug is fixed - the eight 13xxx IDs are requested, `"5"`-`"10"` are dropped for ESS, and `mppt4_*` are diagnostic)
    - _Requirements: 2.1, 2.2_

  - [ ] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-Buggy Device/Config Behavior Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions across string inverters, disabled sensors, batteries, meters, comm modules, and ESS operating-status handling)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 4. Fix Checking tests (per design Testing Strategy)
  - Add focused unit tests in `tests/test_coordinator.py` and `tests/test_sensor.py` per the design's Fix Checking pseudocode (`FOR ALL X WHERE isBugCondition(X)`):
  - `test_ess_requests_hybrid_mppt_points` — ESS + sensors on requests the eight 13xxx IDs (subset assertion)
  - `test_ess_drops_string_inverter_mppt_points` — ESS + sensors on omits `"5"`-`"10"` (collision fix; the 13xxx set and `"5"`-`"10"` are disjoint in the requested points)
  - `test_ess_mppt_codes_are_diagnostic` — `mppt4_voltage`/`mppt4_current` ∈ `_DIAGNOSTIC_CODES`
  - Add `tests/test_const.py` shape test — `ESS_MPPT_DIAGNOSTIC_POINTS` maps the expected 13xxx IDs to the reused `mpptN_*` codes (mirror `test_inverter_diagnostic_points_include_per_string`)
  - Property-based / partial-MPPT (Requirement 2.3): with a mocked ESS realtime response returning only MPPT1/MPPT2 points, assert only those sensors are produced and MPPT3/MPPT4 are silently skipped
  - Run all tests; **EXPECTED OUTCOME**: PASS on fixed code
  - _Requirements: 2.1, 2.2, 2.3_

- [ ] 5. Preservation Checking tests (per design Testing Strategy)
  - Add tests per the design's Preservation Checking pseudocode (`FOR ALL X WHERE NOT isBugCondition(X)`), asserting the fixed `extra_measure_points` equals the original golden set:
  - `test_inverter_mppt_points_unchanged` — `DeviceType.INVERTER` + sensors on still requests `"5"`-`"10"` and per-string IDs (`"96"`/`"70"` .. `"103"`/`"77"`) (Requirement 3.1)
  - ESS operating-status preserved — ESS `extra` contains `"13146"` and not `"29"`, mirroring `test_ess_operating_status_avoids_point29_collision` (Requirement 3.2)
  - Disabled-sensors preserved — inverter requests only `{"29": ...}` and ESS only `{"13146": ...}`, no diagnostic sets (Requirement 3.3)
  - Battery/meter/comm preserved — each requests its existing point set unchanged (Requirement 3.4)
  - String-inverter diagnostic classification preserved — all existing `INVERTER_DIAGNOSTIC_POINTS.values()` remain in `_DIAGNOSTIC_CODES` (Requirement 3.5)
  - Prefer a property-based generator over device type × `enable_device_sensors` combinations, asserting equality with the observed original set for every non-buggy case
  - Run all tests; **EXPECTED OUTCOME**: PASS on fixed code (no regressions)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 6. Checkpoint - Ensure all tests pass
  - Run the full suite (`pytest tests/ --run` equivalent for this repo, e.g. `pytest tests/`) and confirm exploration, preservation, fix-checking, and preservation-checking tasks all pass
  - Confirm the bug condition exploration tests from task 1 (which failed pre-fix) now pass and the preservation tests remain green
  - Ensure all tests pass; ask the user if questions arise

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"], "description": "Bug condition exploration test - FAILS on unfixed code" },
    { "wave": 2, "tasks": ["2"], "description": "Preservation property tests - PASS on unfixed code (observation-first baseline)" },
    { "wave": 3, "tasks": ["3.1"], "description": "const.py: add ESS_MPPT_DIAGNOSTIC_POINTS (prerequisite for 3.2 and 3.3)" },
    { "wave": 4, "tasks": ["3.2", "3.3"], "description": "coordinator.py swap and sensor.py union - independent, may run in parallel" },
    { "wave": 5, "tasks": ["3.4", "3.5"], "description": "Re-run Task 1 (now PASS) and Task 2 (still PASS)" },
    { "wave": 6, "tasks": ["4", "5"], "description": "Fix Checking and Preservation Checking tests" },
    { "wave": 7, "tasks": ["6"], "description": "Checkpoint - full suite green" }
  ]
}
```

```
Task 1 (Bug Condition exploration test - FAILS on unfixed code)
   │   confirms bug exists (no 13xxx requested; "5"-"10" present; mppt4_* not diagnostic)
   ▼
Task 2 (Preservation property tests - PASS on unfixed code)
   │   captures baseline (observation-first) for all non-buggy paths
   ▼
Task 3 (Fix implementation)
   ├─ 3.1 const.py: add ESS_MPPT_DIAGNOSTIC_POINTS
   │       │
   │       ├─────────────┐
   │       ▼             ▼
   ├─ 3.2 coordinator.py │  (imports + uses 3.1)
   │   swap "5"-"10" for │
   │   13xxx (keep "29") │
   │                     │
   └─ 3.3 sensor.py ─────┘  (imports + unions 3.1 values into _DIAGNOSTIC_CODES)
       │
       ├─ 3.4 Re-run Task 1 tests → now PASS (Property 1: Expected Behavior)
       └─ 3.5 Re-run Task 2 tests → still PASS (Property 2: Preservation)
   ▼
Task 4 (Fix Checking tests)   ← depends on 3.1, 3.2, 3.3
   │
Task 5 (Preservation Checking tests)   ← depends on 3.2, 3.3
   ▼
Task 6 (Checkpoint - full suite green)   ← depends on 1,2,3,4,5
```

## Notes

- Tasks 1 and 2 must be completed and run against the UNFIXED code before Task 3.
- 3.1 is a prerequisite for both 3.2 and 3.3 (both import `ESS_MPPT_DIAGNOSTIC_POINTS`); 3.2 and 3.3 are independent of each other and may be done in parallel.
- 3.4/3.5 gate progression: do not proceed to Tasks 4-6 until the Property 1 test passes and the Property 2 tests remain green.
- Repo runs pytest with pytest-asyncio; mirror `test_ess_operating_status_avoids_point29_collision` (coordinator), `test_inverter_diagnostic_points_include_per_string` (const), and the diagnostic-classification tests in `test_sensor.py`.
