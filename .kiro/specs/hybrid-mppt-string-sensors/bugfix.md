# Bugfix Requirements Document

## Introduction

The per-string / MPPT diagnostic-sensor feature (issue #189, ~v3.4.0) surfaces MPPT
voltage/current and per-string voltage/current as diagnostic sensors when a user enables
per-device sensors. It works for SG-RS single-phase string inverters
(`DeviceType.INVERTER`).

On SH-family hybrids reported as `DeviceType.ENERGY_STORAGE_SYSTEM` (ESS) — e.g. the
SH20T from Quad2000 — no MPPT/string sensors appear even with per-device sensors enabled,
despite the MPPT data being visible in the Sungrow cloud for that inverter.

Root cause: the coordinator requests the same point set (`INVERTER_DIAGNOSTIC_POINTS`) for
both `INVERTER` and `ENERGY_STORAGE_SYSTEM` device types. That set only contains
string-inverter MPPT point IDs (`"5"`–`"10"`) and per-string IDs (`"96"`/`"70"` ..
`"103"`/`"77"`). SH-family hybrids report MPPT data under a different point-ID range
(`"13001"`/`"13002"` for MPPT1, `"13105"`/`"13106"` for MPPT2, `"13107"`/`"13108"` for
MPPT3, `"13109"`/`"13110"` for MPPT4) — IDs that already exist in the measure-point
catalog (`measure_points_data.py`). Because the ESS branch never requests the 13xxx IDs,
hybrid inverters return nothing for MPPT sensors.

A related symmetry consideration: `sensor.py` builds `_DIAGNOSTIC_CODES` from
`INVERTER_DIAGNOSTIC_POINTS.values()`, so any new ESS MPPT codes must also be classified
as diagnostic sensors, otherwise they would appear as regular sensors rather than in the
device's Diagnostic section.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the device is a `DeviceType.ENERGY_STORAGE_SYSTEM` (SH-family hybrid), per-device sensors are enabled, and the model reports MPPT data on the 13xxx point IDs THEN the system requests only `INVERTER_DIAGNOSTIC_POINTS` (string-inverter IDs `"5"`–`"10"`) and never requests the hybrid MPPT IDs (`"13001"`/`"13002"`, `"13105"`/`"13106"`, `"13107"`/`"13108"`, `"13109"`/`"13110"`), so no MPPT voltage/current sensors are created.

1.2 WHEN an ESS/hybrid MPPT code would be produced (if requested) THEN the system does not classify it as a diagnostic code because `_DIAGNOSTIC_CODES` is derived only from `INVERTER_DIAGNOSTIC_POINTS.values()`, which does not include the hybrid MPPT codes.

### Expected Behavior (Correct)

2.1 WHEN the device is a `DeviceType.ENERGY_STORAGE_SYSTEM` (SH-family hybrid), per-device sensors are enabled, and the model reports MPPT data on the 13xxx point IDs THEN the system SHALL request the hybrid MPPT point IDs (`"13001"`/`"13002"` = MPPT1 V/I, `"13105"`/`"13106"` = MPPT2 V/I, `"13107"`/`"13108"` = MPPT3 V/I, `"13109"`/`"13110"` = MPPT4 V/I) so the MPPT voltage/current sensors are surfaced the same way string inverters surface theirs.

2.2 WHEN an ESS/hybrid MPPT code is produced THEN the system SHALL classify it as a diagnostic code (included in `_DIAGNOSTIC_CODES`) so it lands in the device page's Diagnostic section rather than the main sensors.

2.3 WHEN the device is a hybrid but the model does not report a given MPPT point (e.g. an unpopulated MPPT3/MPPT4) THEN the system SHALL skip that point silently and create no sensor for it, consistent with the existing per-device builder behavior for string inverters.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the device is a `DeviceType.INVERTER` (string inverter such as an SG-RS) with per-device sensors enabled THEN the system SHALL CONTINUE TO surface its MPPT (`"5"`–`"10"`) and per-string (`"96"`/`"70"` .. `"103"`/`"77"`) diagnostic sensors exactly as before.

3.2 WHEN the device is a `DeviceType.ENERGY_STORAGE_SYSTEM` THEN the system SHALL CONTINUE TO request operating status on `"13146"` and drop the inverter operating-status point `"29"` so the two do not collide on the shared `operating_status` code (#182).

3.3 WHEN per-device sensors are disabled THEN the system SHALL CONTINUE TO not request any per-device diagnostic points for either inverters or hybrids.

3.4 WHEN the device is a `DeviceType.BATTERY`, `DeviceType.COMMUNICATION_MODULE`, or `DeviceType.METER` THEN the system SHALL CONTINUE TO request its existing point set unchanged.

3.5 WHEN a string-inverter diagnostic code is produced THEN the system SHALL CONTINUE TO classify it as a diagnostic code so it lands in the Diagnostic section as before.

## Bug Condition and Properties

### Bug Condition Function

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = (deviceType, enableDeviceSensors, modelReportsHybridMppt)
  OUTPUT: boolean

  // The bug is triggered for hybrid (ESS) devices with per-device sensors enabled
  // whose model actually reports MPPT data on the 13xxx point IDs.
  RETURN X.deviceType = DeviceType.ENERGY_STORAGE_SYSTEM
     AND X.enableDeviceSensors = true
     AND X.modelReportsHybridMppt = true
END FUNCTION
```

### Property: Fix Checking

```pascal
// For every hybrid device that reports MPPT data, the fixed coordinator requests
// the hybrid MPPT point IDs, and the produced MPPT codes are classified diagnostic.
FOR ALL X WHERE isBugCondition(X) DO
  requested ← requestedDiagnosticPoints'(X)
  ASSERT {"13001","13002","13105","13106","13107","13108","13109","13110"} ⊆ requested
  FOR ALL code IN hybridMpptCodes(requested) DO
    ASSERT code IN _DIAGNOSTIC_CODES'
  END FOR
END FOR
```

### Property: Preservation Checking

```pascal
// For every non-buggy input (string inverters, disabled sensors, batteries,
// meters, comm modules, ESS operating-status handling), the fixed code behaves
// identically to the original.
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT requestedDiagnosticPoints(X) = requestedDiagnosticPoints'(X)
  ASSERT _DIAGNOSTIC_CODES(stringInverterCodes) = _DIAGNOSTIC_CODES'(stringInverterCodes)
END FOR
```

Where **F** is the original (unfixed) coordinator/point-selection behavior and **F'** is
the fixed behavior after adding hybrid MPPT point requests and diagnostic classification.
