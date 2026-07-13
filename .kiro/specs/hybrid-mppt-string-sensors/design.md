# Hybrid MPPT / String Sensors Bugfix Design

## Overview

SH-family hybrid inverters that iSolarCloud reports as `DeviceType.ENERGY_STORAGE_SYSTEM`
(ESS) never surface MPPT voltage/current diagnostic sensors, even with per-device sensors
enabled, because the coordinator requests the string-inverter point set
(`INVERTER_DIAGNOSTIC_POINTS`) for both `INVERTER` and `ENERGY_STORAGE_SYSTEM`. That set only
carries the string-inverter MPPT point IDs (`"5"`–`"10"`). Hybrids report MPPT data under a
separate 13xxx point range (`13001`/`13002`, `13105`/`13106`, `13107`/`13108`, `13109`/`13110`)
that is already present in the measure-point catalog but is never requested for ESS devices.

The fix introduces a dedicated hybrid MPPT point map (`ESS_MPPT_DIAGNOSTIC_POINTS`) that maps
the 13xxx IDs to the **same** stable code names string inverters already use
(`mppt1_voltage`, `mppt1_current`, …). The coordinator's ESS branch swaps the string-inverter
MPPT IDs (`"5"`–`"10"`) for the 13xxx IDs so the shared `mpptN_*` codes cannot collide, and the
diagnostic-classification set in `sensor.py` continues to derive automatically because the code
names are reused. All other device handling (string inverters, batteries, meters, comm modules,
disabled sensors, ESS operating-status handling) is left untouched.

## Glossary

- **Bug_Condition (C)**: An ESS/hybrid device with per-device sensors enabled whose model
  reports MPPT data on the 13xxx point IDs, for which no MPPT sensors are produced.
- **Property (P)**: For such devices the coordinator requests the hybrid MPPT point IDs and the
  produced MPPT codes are classified as diagnostic — MPPT voltage/current sensors appear in the
  device's Diagnostic section exactly as they do for string inverters.
- **Preservation**: String inverters, batteries, meters, comm modules, disabled-sensor
  behavior, and ESS operating-status handling (`13146` requested, `29` dropped) all stay
  byte-for-byte identical.
- **`_build_device_data` (per-device fetch)**: The coordinator method in
  `custom_components/sungrow/coordinator.py` (~lines 530-570) that picks the `extra_measure_points`
  map per device type and calls `async_get_device_realtime`.
- **`INVERTER_DIAGNOSTIC_POINTS`**: `dict[str, str]` in `const.py` mapping documented inverter
  point IDs to stable codes; the source of the string-inverter MPPT/string diagnostic sensors and
  of `sensor.py`'s `_DIAGNOSTIC_CODES`.
- **`ESS_MPPT_DIAGNOSTIC_POINTS`** (new): `dict[str, str]` in `const.py` mapping the hybrid 13xxx
  MPPT IDs to the reused `mpptN_voltage` / `mpptN_current` codes.
- **`_DIAGNOSTIC_CODES`**: `frozenset` in `sensor.py` built from
  `INVERTER_DIAGNOSTIC_POINTS.values()` (plus battery/comm codes) that decides which point codes
  get `EntityCategory.DIAGNOSTIC`.
- **`type_id`**: `DeviceType(...).value` for the device currently being fetched.

## Bug Details

### Bug Condition

The bug manifests when a device is an SH-family hybrid (reported as
`DeviceType.ENERGY_STORAGE_SYSTEM`), per-device sensors are enabled, and the model reports MPPT
data on the 13xxx point IDs. In the current `_build_device_data`, the ESS branch reuses
`INVERTER_DIAGNOSTIC_POINTS` (minus point `"29"`), which contains only the string-inverter MPPT
IDs `"5"`–`"10"`. The 13xxx IDs are never added to `extra_measure_points`, so the cloud returns
no MPPT points for the hybrid and no MPPT sensors are ever created.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type (deviceType, enableDeviceSensors, modelReportsHybridMppt)
  OUTPUT: boolean

  RETURN input.deviceType = DeviceType.ENERGY_STORAGE_SYSTEM
         AND input.enableDeviceSensors = true
         AND input.modelReportsHybridMppt = true
         AND requestedDiagnosticPoints(input) does NOT contain the 13xxx MPPT IDs
END FUNCTION
```

### Examples

- **SH20T hybrid, per-device sensors ON, reports MPPT1/MPPT2:** Expected — `mppt1_voltage`,
  `mppt1_current`, `mppt2_voltage`, `mppt2_current` diagnostic sensors appear. Actual — none
  appear because `13001`/`13002`/`13105`/`13106` were never requested.
- **SH hybrid with 4 populated MPPTs:** Expected — MPPT1–4 voltage/current sensors appear.
  Actual — none appear.
- **SH hybrid with only MPPT1/MPPT2 populated (MPPT3/MPPT4 unwired):** Expected — MPPT1/MPPT2
  sensors appear, MPPT3/MPPT4 silently skipped. Actual — none appear.
- **SG-RS string inverter, per-device sensors ON (edge/control):** Expected and Actual — MPPT
  sensors from `"5"`–`"10"` appear. This is the unchanged path and must stay working.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- String inverters (`DeviceType.INVERTER`) continue to surface their MPPT (`"5"`–`"10"`) and
  per-string (`"96"`/`"70"` .. `"103"`/`"77"`) diagnostic sensors exactly as before.
- ESS devices continue to request operating status on `"13146"` and drop the inverter
  operating-status point `"29"` so the two do not collide on the shared `operating_status`
  code (#182).
- Batteries, communication modules, and meters continue to request their existing point sets
  (`BATTERY_DEVICE_POINTS`, `COMM_MODULE_POINTS`, `METER_DEVICE_POINTS`) unchanged.
- With per-device sensors disabled, no per-device diagnostic points are requested for either
  inverters or hybrids (only single operating-status points as today).
- String-inverter diagnostic codes stay classified as diagnostic (Diagnostic section unchanged).

**Scope:**
All inputs that are NOT an ESS/hybrid with per-device sensors enabled and reporting hybrid MPPT
data must be completely unaffected by this fix. This includes:
- Any `DeviceType.INVERTER` device (string inverter) in any configuration.
- Any device when per-device sensors are disabled.
- `DeviceType.BATTERY`, `DeviceType.COMMUNICATION_MODULE`, `DeviceType.METER`, and unmapped types.
- ESS operating-status handling (`13146` present, `29` absent) and the rest of the ESS diagnostic
  set that is not the string-inverter MPPT IDs (grid health, per-string, temperature, etc.).

**Note:** The expected correct behavior is defined in the Correctness Properties section
(Property 1). This section focuses on what must NOT change.

## Hypothesized Root Cause

Based on the bug description and code inspection, the cause is confirmed rather than merely
suspected, but framed as hypotheses for the exploratory phase:

1. **Wrong point IDs requested for ESS (primary cause):** `_build_device_data` reuses
   `INVERTER_DIAGNOSTIC_POINTS` for `ENERGY_STORAGE_SYSTEM`. That map's MPPT entries are the
   string-inverter IDs `"5"`–`"10"`; the hybrid MPPT IDs (`13001`/`13002`, `13105`/`13106`,
   `13107`/`13108`, `13109`/`13110`) are never added, so the cloud returns nothing for hybrid MPPT.

2. **Catalog present but unreferenced:** The 13xxx MPPT IDs already exist in
   `measure_points_data.py` (`13001` "MPPT1 Voltage" V, `13002` "MPPT1 Current" A, etc.), so once
   requested they classify correctly by unit — no catalog change is needed. The gap is purely that
   nothing requests them for ESS.

3. **Diagnostic classification derived from the wrong-only set:** `sensor.py` builds
   `_DIAGNOSTIC_CODES` from `INVERTER_DIAGNOSTIC_POINTS.values()`. If hybrid MPPT used *new* code
   names, they would land in the main sensors instead of Diagnostic. Reusing the existing
   `mpptN_*` code names sidesteps this (they are already in the set), but this must be confirmed.

4. **Code-collision risk if both ID ranges requested (design constraint, not current bug):**
   Because `"5"` and `13001` both map to `mppt1_voltage`, requesting both for one ESS would map two
   different point IDs to the same code and silently overwrite one another in the per-device merge —
   the same class of collision already handled for `operating_status` (`29` vs `13146`, #182).

## Correctness Properties

Property 1: Bug Condition - Hybrid MPPT Sensors Surfaced and Classified Diagnostic

_For any_ input where the bug condition holds (an ESS/hybrid device with per-device sensors
enabled whose model reports MPPT data on the 13xxx IDs), the fixed coordinator SHALL include the
hybrid MPPT point IDs `{"13001","13002","13105","13106","13107","13108","13109","13110"}` in the
requested `extra_measure_points`, and every produced hybrid MPPT code SHALL be a member of
`_DIAGNOSTIC_CODES` so the resulting MPPT voltage/current sensors land in the device's Diagnostic
section. MPPT points the model does not report SHALL be skipped silently (no sensor created),
consistent with the existing string-inverter builder behavior.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Non-Buggy Device/Config Behavior Unchanged

_For any_ input where the bug condition does NOT hold (string inverters, disabled per-device
sensors, batteries, meters, comm modules, unmapped types, and ESS operating-status handling), the
fixed code SHALL produce exactly the same requested point set and the same diagnostic
classification for existing codes as the original code. In particular the fixed ESS branch SHALL
NOT request the string-inverter MPPT IDs `"5"`–`"10"` in addition to the 13xxx IDs, so no two point
IDs map to the same `mpptN_*` code for a single ESS device.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct, three coordinated edits:

**File**: `custom_components/sungrow/const.py`

1. **Add `ESS_MPPT_DIAGNOSTIC_POINTS`**: A `dict[str, str]` mapping the hybrid 13xxx MPPT IDs to
   the reused string-inverter code names so classification, naming, units, and icons all work
   unchanged:
   ```python
   # SH-family hybrids/ESS report MPPT voltage/current on a separate point-ID range
   # than string inverters (#189 follow-up). Reuse the same mpptN_* code names so the
   # existing classification/naming/icon path treats them identically; only the point
   # IDs differ. These are already in the measure-point catalog (units V/A), so they
   # classify by unit automatically.
   ESS_MPPT_DIAGNOSTIC_POINTS: dict[str, str] = {
       "13001": "mppt1_voltage",
       "13002": "mppt1_current",
       "13105": "mppt2_voltage",
       "13106": "mppt2_current",
       "13107": "mppt3_voltage",
       "13108": "mppt3_current",
       "13109": "mppt4_voltage",
       "13110": "mppt4_current",
   }
   ```

**File**: `custom_components/sungrow/coordinator.py`

2. **Import the new map** alongside `INVERTER_DIAGNOSTIC_POINTS`.

3. **Rework the ESS diagnostic branch** so that for `ENERGY_STORAGE_SYSTEM` the string-inverter
   MPPT IDs are removed and the 13xxx IDs merged in (swap, not add), preserving the existing
   `"29"` drop:
   ```python
   if type_id in (DeviceType.INVERTER.value, DeviceType.ENERGY_STORAGE_SYSTEM.value):
       diagnostic = INVERTER_DIAGNOSTIC_POINTS
       if type_id == DeviceType.ENERGY_STORAGE_SYSTEM.value:
           # ESS reports operating status on 13146 (requested above); drop the
           # inverter point 29 so the two don't collide on "operating_status" (#182).
           # ESS also reports MPPT on a separate 13xxx range: drop the string-inverter
           # MPPT IDs (5-10) and merge the hybrid MPPT IDs instead. Both ranges share
           # the mpptN_* codes, so requesting BOTH would map two IDs to one code and
           # silently overwrite each other in the per-device merge.
           string_inverter_mppt_ids = {"5", "6", "7", "8", "9", "10"}
           diagnostic = {
               pid: code
               for pid, code in INVERTER_DIAGNOSTIC_POINTS.items()
               if pid != "29" and pid not in string_inverter_mppt_ids
           }
           diagnostic = {**diagnostic, **ESS_MPPT_DIAGNOSTIC_POINTS}
       extra.update(diagnostic)
   ```
   Notes:
   - The `"29"` drop is preserved exactly (Requirement 3.2).
   - Removing `"5"`–`"10"` for ESS is the collision fix required by Property 2's final clause. It
     is also lean: hybrids do not report those IDs, so excluding them avoids requesting points a
     hybrid never populates while guaranteeing no `mpptN_*` code maps to two IDs.
   - The rest of the ESS set (grid health, per-string, temperature, insulation, etc.) is unchanged.
     Per-string IDs (`"96"`/`"70"` ..) are left in place; they use `string_N_*` codes that do not
     collide with the MPPT codes, and the per-device builder skips any a model does not report
     (Requirement 2.3).

**File**: `custom_components/sungrow/sensor.py`

4. **Confirm diagnostic classification needs no code change**: `ESS_MPPT_DIAGNOSTIC_POINTS` reuses
   `mppt1_voltage`, `mppt1_current`, `mppt2_voltage`, `mppt2_current` — all already present in
   `INVERTER_DIAGNOSTIC_POINTS.values()` (via `"5"`–`"8"`), so they are already in
   `_DIAGNOSTIC_CODES`. `mppt3_*` are also present (`"9"`/`"10"`). Only `mppt4_voltage` /
   `mppt4_current` are **new** code names not present in `INVERTER_DIAGNOSTIC_POINTS`, so they are
   NOT currently in `_DIAGNOSTIC_CODES`.

   Therefore, to satisfy Property 1 for MPPT4, union the new map's values into `_DIAGNOSTIC_CODES`:
   ```python
   from .const import (
       ...
       ESS_MPPT_DIAGNOSTIC_POINTS,
       INVERTER_DIAGNOSTIC_POINTS,
   )
   ...
   _DIAGNOSTIC_CODES = (
       frozenset(INVERTER_DIAGNOSTIC_POINTS.values())
       | frozenset(ESS_MPPT_DIAGNOSTIC_POINTS.values())
       | BATTERY_DIAGNOSTIC_CODES
       | frozenset(COMM_MODULE_POINTS.values())
   )
   ```
   This is safe and idempotent: the mppt1–3 codes are already members, so the union only adds
   `mppt4_voltage` / `mppt4_current`. Doing the union unconditionally also future-proofs the set
   against later code-name changes in `ESS_MPPT_DIAGNOSTIC_POINTS`.

### Why reuse the same code names

`resolve_classification(unit, code, point_id)` classifies the 13xxx points correctly by their
catalog unit (V → `SensorDeviceClass.VOLTAGE`, A → `SensorDeviceClass.CURRENT`) regardless of code
name, so classification does not depend on the code. But naming and icon selection for
`mpptN_voltage` / `mpptN_current` are keyed off the code name; reusing the exact string-inverter
code names means hybrid MPPT sensors get identical names, icons, and units with zero additional
mapping. This is the minimal change and keeps hybrids and string inverters visually consistent.

## Testing Strategy

The repo uses **pytest** (async tests via `pytest-asyncio`), with coordinator tests in
`tests/test_coordinator.py`, const-shape tests in `tests/test_const.py`, and sensor
classification tests in `tests/test_sensor.py`. The existing ESS/collision test
`test_ess_operating_status_avoids_point29_collision` (asserts on the
`extra_measure_points` kwarg captured from a `MagicMock` `plants` service) is the exact pattern to
mirror for the new coordinator assertions.

### Validation Approach

Two phases: first surface counterexamples that demonstrate the bug on the UNFIXED code (no 13xxx
IDs requested for ESS), then verify the fix requests the hybrid MPPT IDs, classifies the codes
diagnostic, and leaves every non-buggy path byte-for-byte identical.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix, confirming
root cause #1 (13xxx IDs never requested for ESS). If a test unexpectedly passes on unfixed code,
re-hypothesize.

**Test Plan**: Instantiate `SungrowPlantCoordinator` with a single
`DeviceType.ENERGY_STORAGE_SYSTEM` device and per-device sensors enabled (`CONF_ENABLE_DEVICE_SENSORS: True`),
mock `plants.async_get_device_realtime`, run `_async_update_data()`, and inspect the captured
`extra_measure_points` kwarg. Run on UNFIXED code to observe the missing IDs.

**Test Cases**:
1. **ESS requests hybrid MPPT IDs**: Assert `{"13001","13002","13105","13106","13107","13108","13109","13110"}`
   is a subset of the requested `extra` (will fail on unfixed code — none are present).
2. **ESS drops string-inverter MPPT IDs**: Assert `"5"`–`"10"` are absent from the ESS `extra`
   (will fail on unfixed code — they are present, which is the collision source).
3. **Hybrid MPPT codes are diagnostic**: Assert `mppt4_voltage` / `mppt4_current` ∈
   `_DIAGNOSTIC_CODES` (will fail on unfixed code — only mppt1–3 present).
4. **Partial-MPPT skip (edge)**: With the mocked realtime response returning only MPPT1/MPPT2
   points, assert only those sensors are produced and MPPT3/MPPT4 are silently skipped.

**Expected Counterexamples**:
- ESS `extra_measure_points` contains no `13xxx` MPPT IDs and still contains `"5"`–`"10"`.
- `mppt4_voltage` / `mppt4_current` not in `_DIAGNOSTIC_CODES`.
- Possible causes surfaced: wrong point IDs requested for ESS, classification set derived from the
  string-inverter-only map.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed coordinator requests
the hybrid MPPT IDs and classifies the produced codes as diagnostic.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  requested := requestedDiagnosticPoints_fixed(input)
  ASSERT {"13001","13002","13105","13106","13107","13108","13109","13110"} SUBSET-OF requested
  ASSERT {"5","6","7","8","9","10"} DISJOINT-FROM requested   // no code collision
  FOR ALL code IN hybridMpptCodes(requested) DO
    ASSERT code IN _DIAGNOSTIC_CODES_fixed
  END FOR
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed coordinator
requests the same point set and classifies existing codes the same as the original.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT requestedDiagnosticPoints_original(input) = requestedDiagnosticPoints_fixed(input)
  ASSERT _DIAGNOSTIC_CODES_original(stringInverterCodes) = _DIAGNOSTIC_CODES_fixed(stringInverterCodes)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation because it generates
many device-type / config combinations automatically and catches edge cases manual tests miss.
Candidate generators: device type ∈ {INVERTER, BATTERY, METER, COMMUNICATION_MODULE, unmapped},
`enable_device_sensors` ∈ {true, false}. For every generated non-ESS-buggy case, assert the fixed
`extra_measure_points` equals what the original produced (captured as a golden set for the
INVERTER-with-sensors case, e.g. still contains `"5"`–`"10"` and the per-string IDs).

**Test Plan**: Observe UNFIXED behavior for string inverters, disabled sensors, batteries, meters,
and comm modules first, then assert the fixed code reproduces it.

**Test Cases**:
1. **String-inverter MPPT/string preserved**: `DeviceType.INVERTER` with sensors on still requests
   `"5"`–`"10"` and `"96"`/`"70"` .. `"103"`/`"77"` (Requirement 3.1).
2. **ESS operating-status handling preserved**: ESS `extra` contains `"13146"` and not `"29"`,
   mirroring `test_ess_operating_status_avoids_point29_collision` (Requirement 3.2).
3. **Disabled sensors preserved**: With per-device sensors off, inverter requests only `{"29": ...}`
   and ESS only `{"13146": ...}`; no diagnostic sets (Requirement 3.3).
4. **Battery/meter/comm preserved**: Each requests its existing point set unchanged (Requirement 3.4).
5. **String-inverter diagnostic classification preserved**: All existing
   `INVERTER_DIAGNOSTIC_POINTS` codes remain in `_DIAGNOSTIC_CODES` (Requirement 3.5).

### Unit Tests

- `test_ess_requests_hybrid_mppt_points` — ESS + sensors on requests the eight 13xxx IDs.
- `test_ess_drops_string_inverter_mppt_points` — ESS + sensors on omits `"5"`–`"10"` (collision fix).
- `test_ess_mppt_codes_are_diagnostic` — `mppt4_voltage`/`mppt4_current` ∈ `_DIAGNOSTIC_CODES`.
- `test_inverter_mppt_points_unchanged` — INVERTER still requests `"5"`–`"10"`.
- `test_const.py` shape test — `ESS_MPPT_DIAGNOSTIC_POINTS` maps the expected IDs to the reused
  `mpptN_*` codes (mirrors `test_inverter_diagnostic_points_include_per_string`).

### Property-Based Tests

- Generate device type × `enable_device_sensors` combinations; assert the fixed
  `extra_measure_points` for every non-buggy combination equals the original (preservation).
- Generate ESS realtime responses with random subsets of MPPT1–4 populated; assert exactly the
  populated MPPT sensors are produced and unpopulated ones are skipped (Requirement 2.3).
- Assert for ESS the requested IDs never contain both a `"5"`–`"10"` ID and its 13xxx counterpart
  (no two IDs share a `mpptN_*` code).

### Integration Tests

- Full `_async_update_data()` flow (HomeAssistant fixture, as in
  `test_inverter_diagnostic_sensor_is_diagnostic_and_enum`) for an ESS device with per-device
  sensors on: assert `SungrowDeviceSensor` entities for `mppt1_voltage`/`mppt1_current` etc. are
  created with `EntityCategory.DIAGNOSTIC`, correct device/state class (VOLTAGE/CURRENT), and the
  expected names/icons/units matching string inverters.
- Mixed-plant flow: one `INVERTER` and one `ENERGY_STORAGE_SYSTEM` on the same plant — assert the
  string inverter surfaces its `"5"`–`"10"` MPPT sensors and the hybrid surfaces its 13xxx MPPT
  sensors, with no cross-device code collision.
- Regression: ESS operating-status (`13146`) and battery points still surface as before.
