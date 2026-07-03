# Comprehensive measuring-point mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every iSolarCloud measure point a friendly English name and a correct device/state class — including the dimensionless points (SOC, SOH, power factor, PR, counts) that currently degrade to text sensors — grounded in the official mcp-isolarcloud catalogs.

**Architecture:** A new data module (`measure_points_data.py`) holds the transcribed catalog rows, enum tables and code aliases; a new logic module (`measure_points.py`) builds a point-ID-keyed `POINT_CATALOG` and exposes pure resolver functions. `sensor.py` calls the resolvers instead of its inline naming/classification logic. The API's real unit stays authoritative; the catalog and code-keyword rules are fallbacks for unitless points.

**Tech Stack:** Python 3.13, Home Assistant `SensorDeviceClass`/`SensorStateClass`, pytest, ruff, mypy. No new runtime dependencies.

## Global Constraints

- Python 3.13; ruff line length 120; ruff format; mypy must pass (CLAUDE.md).
- Conventional Commits for every commit (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
- Every behaviour change needs tests; keep `pyproject.toml` `fail_under` coverage green.
- Work on branch `feat/comprehensive-measure-point-mapping`; do NOT push to `main`; open a PR.
- `pysolarcloud` stays pinned in `manifest.json`; do not touch the token-persistence path.
- Data source of truth: mcp-isolarcloud docs server (`read_doc` on each `common-*-measuring-points` slug). Captured snapshot: `scratchpad/measure_points_catalog.tsv` (this session).
- Enum sensors: `device_class = SensorDeviceClass.ENUM`, `_attr_options` set, **no** unit, **no** state class; `native_value` returns one of the options (or `str(raw)` for an unmapped code).

---

## File structure

| File | Responsibility |
| --- | --- |
| `custom_components/sungrow/measure_points_data.py` (create) | Pure data: `RAW_POINTS: list[tuple[str,str,str]]` (point_id, name, unit) for all catalogs; `ENUM_MAPS: dict[str, dict[int,str]]`; `CODE_ALIASES: dict[str,str]`. No logic, no HA imports. |
| `custom_components/sungrow/measure_points.py` (create) | `PointInfo` type; `_UNIT_CLASS_MAP` + `_classify_by_unit`; build-time `_classify_point`; `POINT_CATALOG`; resolvers `resolve_name`, `resolve_classification`, `resolve_enum_options`, `resolve_enum_value`. |
| `custom_components/sungrow/sensor.py` (modify) | Delegate naming/classification/enum to `measure_points`; drop the local `SENSOR_ALIASES`/`EXTRA_CODE_ALIASES`/`_UNIT_CLASS_MAP`; add enum branch to `native_value`. |
| `tests/test_measure_points.py` (create) | Unit tests for classifiers, resolvers, enum, catalog integrity. |
| `tests/test_sensor.py` (modify) | Update `infer_device_class` cases for new signature; add enum + unitless-numeric `native_value` cases; naming-precedence cases. |
| `docs/SENSORS.md` (modify) | Comprehensive per-device-type `point_id=code` blocks + automatic-classification note. |

---

## Task 1: `measure_points.py` core — `PointInfo` + unit classifier

**Files:**
- Create: `custom_components/sungrow/measure_points.py`
- Create: `tests/test_measure_points.py`

**Interfaces:**
- Produces: `PointInfo(NamedTuple)` with fields `name: str`, `device_class: SensorDeviceClass | None`, `state_class: SensorStateClass | None`, `options: tuple[str, ...] | None = None`.
- Produces: `_classify_by_unit(unit: str | None) -> tuple[SensorDeviceClass | None, SensorStateClass | None] | None` — returns `None` when the unit is unknown (so callers can fall through), a 2-tuple when known.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_measure_points.py
"""Tests for the Sungrow measuring-point catalog and resolvers."""

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.sungrow import measure_points as mp

M = SensorStateClass.MEASUREMENT
TI = SensorStateClass.TOTAL_INCREASING


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("W", (SensorDeviceClass.POWER, M)),
        ("kWh", (SensorDeviceClass.ENERGY, TI)),
        ("V", (SensorDeviceClass.VOLTAGE, M)),
        ("mV", (SensorDeviceClass.VOLTAGE, M)),
        ("A", (SensorDeviceClass.CURRENT, M)),
        ("Hz", (SensorDeviceClass.FREQUENCY, M)),
        ("°C", (SensorDeviceClass.TEMPERATURE, M)),
        ("var", (SensorDeviceClass.REACTIVE_POWER, M)),
        ("VA", (SensorDeviceClass.APPARENT_POWER, M)),
        # New units for the broader catalogs.
        ("%RH", (SensorDeviceClass.HUMIDITY, M)),
        ("m/s", (SensorDeviceClass.WIND_SPEED, M)),
        ("hPa", (SensorDeviceClass.PRESSURE, M)),
        ("mm", (SensorDeviceClass.PRECIPITATION, M)),
        ("W/m²", (SensorDeviceClass.IRRADIANCE, M)),
        ("h", (SensorDeviceClass.DURATION, M)),
        ("varh", (None, TI)),
        ("kΩ", (None, M)),
        # Case/space-insensitive.
        ("kwh", (SensorDeviceClass.ENERGY, TI)),
        (" W ", (SensorDeviceClass.POWER, M)),
    ],
)
def test_classify_by_unit_known(unit, expected):
    assert mp._classify_by_unit(unit) == expected


@pytest.mark.parametrize("unit", ["", None, "widgets", "%"])
def test_classify_by_unit_unknown_returns_none(unit):
    assert mp._classify_by_unit(unit) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError: module 'measure_points' has no attribute '_classify_by_unit'`.

- [ ] **Step 3: Write minimal implementation**

```python
# custom_components/sungrow/measure_points.py
"""Catalog and resolver helpers for iSolarCloud measure points.

Naming and classification for the sensors built in ``sensor.py``. The API's
reported unit stays authoritative; this module supplies English names and a
classification fallback for the many documented points that are dimensionless
(SOC, SOH, power factor, PR, counts) and would otherwise become text sensors.

Data (catalog rows, enum tables, code aliases) lives in ``measure_points_data``.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

_MEASUREMENT = SensorStateClass.MEASUREMENT
_TOTAL_INCREASING = SensorStateClass.TOTAL_INCREASING

_ClassPair = tuple[SensorDeviceClass | None, SensorStateClass | None]


class PointInfo(NamedTuple):
    """Resolved metadata for one measure point."""

    name: str
    device_class: SensorDeviceClass | None
    state_class: SensorStateClass | None
    options: tuple[str, ...] | None = None


# Unit (lower-cased, stripped) -> (device_class, state_class). Energy is
# TOTAL_INCREASING so cumulative and daily-reset counters both feed the Energy
# dashboard (issue #19).
_UNIT_CLASS_MAP: dict[str, _ClassPair] = {
    # Power
    "w": (SensorDeviceClass.POWER, _MEASUREMENT),
    "kw": (SensorDeviceClass.POWER, _MEASUREMENT),
    "mw": (SensorDeviceClass.POWER, _MEASUREMENT),
    "gw": (SensorDeviceClass.POWER, _MEASUREMENT),
    # Energy
    "wh": (SensorDeviceClass.ENERGY, _TOTAL_INCREASING),
    "kwh": (SensorDeviceClass.ENERGY, _TOTAL_INCREASING),
    "mwh": (SensorDeviceClass.ENERGY, _TOTAL_INCREASING),
    "gwh": (SensorDeviceClass.ENERGY, _TOTAL_INCREASING),
    # Voltage
    "v": (SensorDeviceClass.VOLTAGE, _MEASUREMENT),
    "mv": (SensorDeviceClass.VOLTAGE, _MEASUREMENT),
    "kv": (SensorDeviceClass.VOLTAGE, _MEASUREMENT),
    # Current
    "a": (SensorDeviceClass.CURRENT, _MEASUREMENT),
    "ma": (SensorDeviceClass.CURRENT, _MEASUREMENT),
    # Frequency
    "hz": (SensorDeviceClass.FREQUENCY, _MEASUREMENT),
    # Temperature
    "°c": (SensorDeviceClass.TEMPERATURE, _MEASUREMENT),
    "℃": (SensorDeviceClass.TEMPERATURE, _MEASUREMENT),
    "c": (SensorDeviceClass.TEMPERATURE, _MEASUREMENT),
    "°f": (SensorDeviceClass.TEMPERATURE, _MEASUREMENT),
    # Reactive / apparent power
    "var": (SensorDeviceClass.REACTIVE_POWER, _MEASUREMENT),
    "kvar": (SensorDeviceClass.REACTIVE_POWER, _MEASUREMENT),
    "va": (SensorDeviceClass.APPARENT_POWER, _MEASUREMENT),
    "kva": (SensorDeviceClass.APPARENT_POWER, _MEASUREMENT),
    # Environment / meter extras (comprehensive catalogs).
    "%rh": (SensorDeviceClass.HUMIDITY, _MEASUREMENT),
    "m/s": (SensorDeviceClass.WIND_SPEED, _MEASUREMENT),
    "hpa": (SensorDeviceClass.PRESSURE, _MEASUREMENT),
    "mm": (SensorDeviceClass.PRECIPITATION, _MEASUREMENT),
    "w/m²": (SensorDeviceClass.IRRADIANCE, _MEASUREMENT),
    "w/㎡": (SensorDeviceClass.IRRADIANCE, _MEASUREMENT),
    "w/m^2": (SensorDeviceClass.IRRADIANCE, _MEASUREMENT),
    "h": (SensorDeviceClass.DURATION, _MEASUREMENT),
    # Numeric-but-no-HA-device-class units: classify as plain numeric so they graph.
    "varh": (None, _TOTAL_INCREASING),
    "kω": (None, _MEASUREMENT),
    "ω": (None, _MEASUREMENT),
    "°": (None, _MEASUREMENT),
    "wh/m²": (None, _TOTAL_INCREASING),
    "wh/㎡": (None, _TOTAL_INCREASING),
    "wh/m^2": (None, _TOTAL_INCREASING),
    "w/wp": (None, _MEASUREMENT),
}


def _classify_by_unit(unit: str | None) -> _ClassPair | None:
    """Return the class pair for a known unit, else ``None``."""
    if not unit:
        return None
    return _UNIT_CLASS_MAP.get(unit.strip().lower())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: PASS (all parametrized cases green).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/measure_points.py tests/test_measure_points.py
git commit -m "feat: add measure-point unit classifier core (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Enum data + enum resolvers

**Files:**
- Create: `custom_components/sungrow/measure_points_data.py`
- Modify: `custom_components/sungrow/measure_points.py`
- Modify: `tests/test_measure_points.py`

**Interfaces:**
- Produces (data): `ENUM_MAPS: dict[str, dict[int, str]]` keyed by point-ID string.
- Produces: `resolve_enum_options(point_id: str) -> tuple[str, ...] | None` — distinct labels in map order, or `None`.
- Produces: `resolve_enum_value(point_id: str, value: Any) -> str | None` — label for an int-coercible value, `None` if the point isn't an enum, `str(value)` if the code is unmapped.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_measure_points.py
def test_enum_options_charger_status():
    opts = mp.resolve_enum_options("33716")
    assert opts is not None
    assert "Charging" in opts
    assert "Idle (not plugged in)" in opts
    # Distinct + order-preserving.
    assert len(opts) == len(set(opts))


def test_enum_options_none_for_non_enum():
    assert mp.resolve_enum_options("8018") is None


def test_enum_value_maps_int():
    assert mp.resolve_enum_value("33716", 3) == "Charging"
    assert mp.resolve_enum_value("33716", "3") == "Charging"
    assert mp.resolve_enum_value("33716", 3.0) == "Charging"


def test_enum_value_unmapped_code_falls_back_to_str():
    assert mp.resolve_enum_value("33716", 999) == "999"


def test_enum_value_none_for_non_enum():
    assert mp.resolve_enum_value("8018", 5) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'resolve_enum_options'`.

- [ ] **Step 3: Write minimal implementation**

Create the data module with the four documented enum tables (values transcribed from the `common-charger`, `common-energy-storage-inverter`, `common-inverter`, `common-microinverter` catalogs). `_OPERATING_STATUS` is shared by inverter point 29 and ESS-inverter point 13146 (same documented family):

```python
# custom_components/sungrow/measure_points_data.py
"""Static measure-point data transcribed from the iSolarCloud docs.

Source: mcp-isolarcloud docs server (``common-*-measuring-points`` pages).
No logic here — see ``measure_points.py``.
"""

from __future__ import annotations

# --- Enum value tables (point_id -> {int_code: label}) ------------------------

_CHARGER_STATUS: dict[int, str] = {
    1: "Idle (not plugged in)",
    2: "Standby (plugged in)",
    3: "Charging",
    4: "Charging paused (station)",
    5: "Charging paused (vehicle)",
    6: "Charging completed",
    7: "Reserved",
    8: "Disabled",
    9: "Fault",
}

# Inverter operating status (point 29) and energy-storage-inverter operating
# status (point 13146) share this documented family.
_OPERATING_STATUS: dict[int, str] = {
    0: "Grid-connected operation",
    20: "Microgrid operation",
    64: "Grid-connected, operating normally",
    65: "Off-grid charging",
    128: "Derated running",
    256: "Operation fault",
    512: "Update failed",
    1024: "Maintenance mode",
    2048: "Forced mode",
    4096: "Off-grid",
    4369: "Uninitialized",
    4608: "Initial standby",
    4864: "Awaiting startup",
    5120: "Standby",
    5376: "Emergency stop",
    5632: "Starting up",
    5888: "AFCI self-test",
    6144: "Smart plant building status",
    6400: "Security mode",
    8192: "Open loop",
    9472: "LCD/DSP communication fault",
    9473: "Restarting",
    16384: "Energy dispatch mode",
    16385: "Emergency charging mode",
    16435: "Low insulation resistance",
    16439: "Insulation board anomaly",
    20992: "Shutting down",
    21760: "Shut down due to faults",
    32768: "Shut down",
    33024: "Derated running",
    33280: "Dispatched running",
    33792: "Running under anti-PID condition",
    37120: "Running with alarm",
}

_MICROINVERTER_STATUS: dict[int, str] = {
    0: "On-grid operation",
    1024: "Maintenance mode",
    2048: "Forced mode",
    4096: "Backup operation",
    4369: "Initial status",
    4608: "Initial standby",
    4864: "Press to shut down",
    5120: "Standby",
    5632: "Starting up",
    8192: "Open loop",
    21760: "Shut down due to faults",
    32768: "Shutdown",
    33024: "Derating running",
    33280: "Dispatch running",
    37120: "Warn run",
}

ENUM_MAPS: dict[str, dict[int, str]] = {
    "33716": _CHARGER_STATUS,
    "29": _OPERATING_STATUS,
    "13146": _OPERATING_STATUS,
    "51301": _MICROINVERTER_STATUS,
}

# RAW_POINTS and CODE_ALIASES are appended in later tasks.
RAW_POINTS: list[tuple[str, str, str]] = []
CODE_ALIASES: dict[str, str] = {}
```

Add resolvers to `measure_points.py` (import the data at the top):

```python
# near the top imports of measure_points.py
from .measure_points_data import CODE_ALIASES, ENUM_MAPS, RAW_POINTS

# ... after _classify_by_unit ...

def resolve_enum_options(point_id: str) -> tuple[str, ...] | None:
    """Return the distinct enum labels for an enum point, else ``None``."""
    mapping = ENUM_MAPS.get(point_id)
    if not mapping:
        return None
    # dict preserves insertion order; dedupe labels while keeping order.
    return tuple(dict.fromkeys(mapping.values()))


def resolve_enum_value(point_id: str, value: Any) -> str | None:
    """Map a raw value to its enum label.

    Returns ``None`` when the point is not an enum, the mapped label when the
    value is a known code, and ``str(value)`` when the point is an enum but the
    code is not in the table (forward-compatible with new firmware codes).
    """
    mapping = ENUM_MAPS.get(point_id)
    if not mapping:
        return None
    try:
        code = int(float(value))
    except (ValueError, TypeError):
        return str(value)
    return mapping.get(code, str(value))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/measure_points_data.py custom_components/sungrow/measure_points.py tests/test_measure_points.py
git commit -m "feat: add documented status-enum tables and resolvers (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Runtime classification — `resolve_classification`

**Files:**
- Modify: `custom_components/sungrow/measure_points.py`
- Modify: `tests/test_measure_points.py`

**Interfaces:**
- Produces: `resolve_classification(unit: str | None, code: str, point_id: str) -> tuple[SensorDeviceClass | None, SensorStateClass | None]`.
- Consumes: `_classify_by_unit` (Task 1), `ENUM_MAPS` (Task 2), and `POINT_CATALOG` (defined in Task 4; referenced by name and resolved at call time — the catalog-fallback branch is exercised by a test in Task 4).

Resolution order: enum → known unit → percent (SOH/SOC carve-outs) → catalog fallback → code keyword → `(None, None)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_measure_points.py
def test_classify_enum_point():
    assert mp.resolve_classification("", "ev_charger_status", "33716") == (
        SensorDeviceClass.ENUM,
        None,
    )


def test_classify_unit_wins():
    assert mp.resolve_classification("kWh", "anything", "0") == (
        SensorDeviceClass.ENERGY,
        TI,
    )


def test_classify_percent_battery():
    assert mp.resolve_classification("%", "battery_soc", "0") == (SensorDeviceClass.BATTERY, M)


def test_classify_percent_soh_is_not_battery():
    # SOH is health, not charge level — must NOT be BATTERY device class.
    assert mp.resolve_classification("%", "battery_soh", "0") == (None, M)


def test_classify_percent_generic():
    assert mp.resolve_classification("%", "efficiency", "0") == (None, M)


def test_classify_dimensionless_power_factor_by_code():
    assert mp.resolve_classification("", "meter_power_factor", "0") == (
        SensorDeviceClass.POWER_FACTOR,
        M,
    )


def test_classify_dimensionless_soc_by_code():
    assert mp.resolve_classification(None, "total_field_soc", "0") == (SensorDeviceClass.BATTERY, M)


def test_classify_unknown_is_text():
    assert mp.resolve_classification("", "some_status", "0") == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: FAIL — `AttributeError: ... 'resolve_classification'`.

- [ ] **Step 3: Write minimal implementation**

```python
# in measure_points.py, after the enum resolvers

# Percentage codes that mean charge level (battery device class) vs. health.
_SOC_HINTS = ("soc", "battery_level", "state_of_charge")
_SOH_HINTS = ("soh", "health")
_POWER_FACTOR_HINTS = ("power_factor",)


def _classify_percent(code: str) -> _ClassPair:
    """Classify a ``%`` point using its code (charge vs. health vs. generic)."""
    lowered = code.lower()
    if any(h in lowered for h in _SOH_HINTS):
        return (None, _MEASUREMENT)
    if any(h in lowered for h in _SOC_HINTS) or "battery" in lowered or "capacity" in lowered:
        return (SensorDeviceClass.BATTERY, _MEASUREMENT)
    return (None, _MEASUREMENT)


def _classify_by_code(code: str) -> _ClassPair | None:
    """Fallback classification from a dimensionless code, else ``None``."""
    lowered = code.lower()
    if any(h in lowered for h in _POWER_FACTOR_HINTS):
        return (SensorDeviceClass.POWER_FACTOR, _MEASUREMENT)
    if any(h in lowered for h in _SOH_HINTS):
        return (None, _MEASUREMENT)
    if any(h in lowered for h in _SOC_HINTS):
        return (SensorDeviceClass.BATTERY, _MEASUREMENT)
    return None


def resolve_classification(unit: str | None, code: str, point_id: str) -> _ClassPair:
    """Resolve (device_class, state_class) for a measure point.

    The API unit is authoritative; the catalog and code keywords are fallbacks
    for the many documented points that report no unit.
    """
    if point_id in ENUM_MAPS:
        return (SensorDeviceClass.ENUM, None)

    by_unit = _classify_by_unit(unit)
    if by_unit is not None:
        return by_unit

    if unit and unit.strip().lower() in ("%", "percent"):
        return _classify_percent(code)

    info = POINT_CATALOG.get(point_id)
    if info is not None and (info.device_class is not None or info.state_class is not None):
        return (info.device_class, info.state_class)

    by_code = _classify_by_code(code)
    if by_code is not None:
        return by_code

    return (None, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: PASS. (These cases avoid the catalog-fallback branch — `POINT_CATALOG` is empty until Task 4; the reference resolves at call time so it must already be importable. Add a temporary `POINT_CATALOG: dict[str, PointInfo] = {}` near the bottom of the module now; Task 4 replaces the assignment with the real build.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/measure_points.py tests/test_measure_points.py
git commit -m "feat: add runtime measure-point classification resolver (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Catalog data (`RAW_POINTS`) + `POINT_CATALOG` build + integrity

**Files:**
- Modify: `custom_components/sungrow/measure_points_data.py` (populate `RAW_POINTS`)
- Modify: `custom_components/sungrow/measure_points.py` (build `POINT_CATALOG`, add `_classify_point`)
- Modify: `tests/test_measure_points.py`

**Interfaces:**
- Produces (data): `RAW_POINTS` — one `(point_id, name, unit)` tuple per documented point, transcribed verbatim from every `common-*-measuring-points` catalog. Blank unit = `""`.
- Produces: `_classify_point(name: str, unit: str, point_id: str) -> _ClassPair` — build-time classifier (unit → `_classify_by_unit`; blank unit → name keywords; enum point → `(ENUM, None)`).
- Produces: `POINT_CATALOG: dict[str, PointInfo]` — `RAW_POINTS` mapped through `_classify_point`, with `options` set for enum points.

**Populating `RAW_POINTS`:** transcribe each catalog with `mcp-isolarcloud read_doc` (slugs below). The captured snapshot is in `scratchpad/measure_points_catalog.tsv`. Clean the doc superscripts (`W/m^2^` → `W/m²`). Catalogs and their point-ID ranges:
`common-battery` (58601–58636), `common-charger` (33702–33729), `common-energy-meter` (8000–8085), `common-ems-device` (24620–24631), `common-energy-storage-inverter` (13001–18110), `common-inverter` (1–326, 7xxx), `common-plant` (83001–83743), `common-combiner-box` (1001–1032), `common-pcs-device` (44010–44824), `common-cmu-device` (59002–59403), `common-bsc-device` (59008–59067; **skip** 59008/59010/59012/59014 already added from CMU — dedupe by ID), `common-lc-device` (59502–59705), `common-microinverter` (51301–51352), `common-environment-monitoring-device` (2001–2139), `common-communications-device` (10026–10587), `common-communications-module` (23001–23014), `common-ihomemanage` (88016–88035).

The high-value/irregular head of `RAW_POINTS` is given verbatim below; continue the same `(id, name, unit)` pattern for the remaining catalogs. A completeness test (Step 1) asserts the minimum count and key anchors so omissions fail CI.

```python
# in measure_points_data.py — replace the empty RAW_POINTS with the full list.
# (Battery / charger / energy-meter / EMS shown in full; append the remaining
#  catalogs in the same 3-tuple (point_id, name, unit) shape.)
RAW_POINTS: list[tuple[str, str, str]] = [
    # --- Battery (common-battery-measuring-points) ---
    ("58601", "Battery Voltage", "V"),
    ("58602", "Battery Current", "A"),
    ("58603", "Battery Temperature", "°C"),
    ("58604", "Battery Level", ""),
    ("58605", "Battery Health (SOH)", ""),
    ("58606", "Total Battery Charging Energy", "Wh"),
    ("58607", "Total Battery Discharging Energy", "Wh"),
    ("58608", "Battery Operation Status", ""),
    ("58609", "Standard Health Status", ""),
    ("58610", "Max. Voltage of Cell", "mV"),
    ("58611", "Position of Max-Voltage Cell", ""),
    ("58612", "Min. Voltage of Cell", "mV"),
    ("58613", "Position of Min-Voltage Cell", ""),
    ("58614", "Max. Temperature of Module", "°C"),
    ("58615", "Max. Temperature Position of Module", ""),
    ("58616", "Min. Temperature of Module", "°C"),
    ("58617", "Min. Temperature Position of Module", ""),
    ("58618", "Max. Cell Voltage of Module 1", "mV"),
    ("58619", "Max. Cell Voltage of Module 2", "mV"),
    ("58620", "Max. Cell Voltage of Module 3", "mV"),
    ("58621", "Max. Cell Voltage of Module 4", "mV"),
    ("58622", "Max. Cell Voltage of Module 5", "mV"),
    ("58623", "Max. Cell Voltage of Module 6", "mV"),
    ("58624", "Max. Cell Voltage of Module 7", "mV"),
    ("58625", "Max. Cell Voltage of Module 8", "mV"),
    ("58626", "Min. Cell Voltage of Module 1", "mV"),
    ("58627", "Min. Cell Voltage of Module 2", "mV"),
    ("58628", "Min. Cell Voltage of Module 3", "mV"),
    ("58629", "Min. Cell Voltage of Module 4", "mV"),
    ("58630", "Min. Cell Voltage of Module 5", "mV"),
    ("58631", "Min. Cell Voltage of Module 6", "mV"),
    ("58632", "Min. Cell Voltage of Module 7", "mV"),
    ("58633", "Min. Cell Voltage of Module 8", "mV"),
    ("58635", "DC Contactor Status", ""),
    ("58636", "Fault Module ID", ""),
    # --- EV charger (common-charger-measuring-points) ---
    ("33708", "Charging Power", "kW"),
    ("33722", "Min. Charge Power", "kW"),
    ("33723", "Max. Charging Power", "kW"),
    ("33702", "Phase A Charging Voltage", "V"),
    ("33704", "Phase B Charging Voltage", "V"),
    ("33706", "Phase C Charging Voltage", "V"),
    ("33710", "CP Voltage", "V"),
    ("33703", "Phase A Charging Current", "A"),
    ("33705", "Phase B Charging Current", "A"),
    ("33707", "Phase C Charging Current", "A"),
    ("33728", "Max. Charging Current", "A"),
    ("33729", "Min. Charge Current", "A"),
    ("33716", "Charging Status", ""),
    # --- Energy meter (common-energy-meter-measuring-points) ---
    ("8030", "Forward Active Energy", "Wh"),
    ("8031", "Reverse Active Energy", "Wh"),
    ("8062", "Daily Forward Active Energy", "Wh"),
    ("8063", "Daily Reverse Active Energy", "Wh"),
    ("8032", "Forward Reactive Energy", "varh"),
    ("8033", "Reverse Reactive Energy", "varh"),
    ("8034", "Peak Forward Active Energy", "Wh"),
    ("8035", "Peak Reverse Active Energy", "Wh"),
    ("8038", "Valley Forward Active Energy", "Wh"),
    ("8039", "Valley Reverse Active Energy", "Wh"),
    ("8042", "Flat Forward Active Energy", "Wh"),
    ("8043", "Flat Reverse Active Energy", "Wh"),
    ("8058", "Wave Forward Active Energy", "Wh"),
    ("8059", "Wave Reverse Active Energy", "Wh"),
    ("8000", "Phase A Voltage", "V"),
    ("8001", "Phase B Voltage", "V"),
    ("8002", "Phase C Voltage", "V"),
    ("8003", "A-B Line Voltage", "V"),
    ("8004", "B-C Line Voltage", "V"),
    ("8005", "C-A Line Voltage", "V"),
    ("8006", "Phase A Current", "A"),
    ("8007", "Phase B Current", "A"),
    ("8008", "Phase C Current", "A"),
    ("8064", "Frequency", "Hz"),
    ("8018", "Meter Active Power", "W"),
    ("8022", "Reactive Power", "var"),
    ("8014", "Power Factor", ""),
    ("8026", "Apparent Power", "VA"),
    ("8076", "Meter Phase A Active Power", "W"),
    ("8077", "Meter Phase B Active Power", "W"),
    ("8078", "Meter Phase C Active Power", "W"),
    ("8084", "Daily Direct Power Consumption", "Wh"),
    ("8085", "Total Direct Power Consumption", "Wh"),
    # --- EMS device (common-ems-device-measuring-points) ---
    ("24620", "ESS Daily Charge", "Wh"),
    ("24621", "ESS Daily Discharge", "Wh"),
    ("24622", "ESS Total Charge", "Wh"),
    ("24623", "ESS Total Discharge", "Wh"),
    ("24624", "PV Active Power", "W"),
    ("24625", "Energy Storage Active Power", "W"),
    ("24626", "Grid Active Power", "W"),
    ("24627", "Daily PV Yield", "Wh"),
    ("24628", "Total PV Yield", "Wh"),
    ("24629", "Energy Storage SOC", "%"),
    ("24630", "Energy Storage Remaining Charge", "Wh"),
    ("24631", "Active Load", "W"),
    # --- Remaining catalogs: append (id, name, unit) for
    #     energy-storage-inverter, inverter, plant, combiner-box, pcs, cmu, bsc,
    #     lc, microinverter, environment, comms-device, comms-module, ihomemanage.
    #     Transcribe from mcp-isolarcloud (see slug list above); dedupe by point_id.
]
```

- [ ] **Step 1: Write the failing tests (integrity + classification via catalog)**

```python
# append to tests/test_measure_points.py
def test_catalog_covers_all_device_types():
    ids = {pid for pid, _n, _u in mp.RAW_POINTS}
    # Anchor points from each catalog must be present.
    for pid in ("58604", "33716", "8014", "24629", "13141", "24", "83252", "51301", "2016", "44012"):
        assert pid in ids, f"missing catalog point {pid}"
    # Comprehensive coverage — the union of catalogs is well over 300 points.
    assert len(mp.RAW_POINTS) >= 300


def test_catalog_no_duplicate_ids():
    ids = [pid for pid, _n, _u in mp.RAW_POINTS]
    assert len(ids) == len(set(ids)), "duplicate point IDs in RAW_POINTS"


def test_catalog_entry_names_and_classes():
    info = mp.POINT_CATALOG["58601"]  # Battery Voltage, V
    assert info.name == "Battery Voltage"
    assert info.device_class == SensorDeviceClass.VOLTAGE
    # Dimensionless SOH is numeric, not a battery device class.
    assert mp.POINT_CATALOG["58605"].device_class is None
    assert mp.POINT_CATALOG["58605"].state_class == M
    # Dimensionless power factor.
    assert mp.POINT_CATALOG["8014"].device_class == SensorDeviceClass.POWER_FACTOR
    # Battery level -> battery device class even with no unit.
    assert mp.POINT_CATALOG["58604"].device_class == SensorDeviceClass.BATTERY


def test_enum_maps_reference_real_catalog_points():
    for pid in mp.ENUM_MAPS:
        assert pid in mp.POINT_CATALOG, f"enum point {pid} not in catalog"
        assert mp.POINT_CATALOG[pid].options, f"enum point {pid} missing options"


def test_classify_uses_catalog_fallback_for_unitless():
    # No unit passed at runtime, but the catalog knows 8014 is a power factor.
    assert mp.resolve_classification(None, "8014", "8014") == (SensorDeviceClass.POWER_FACTOR, M)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: FAIL — empty catalog / missing anchors / `KeyError`.

- [ ] **Step 3: Implement — full `RAW_POINTS` + `_classify_point` + `POINT_CATALOG`**

Populate `RAW_POINTS` fully (all catalogs, per the slug list). Then in `measure_points.py`:

```python
# name-keyword classifier for dimensionless catalog points (build time).
_TEXT_NAME_HINTS = (
    "status", "mode", "version", "s/n", "serial", "card id", "position",
    " id", "device s", "working", "running", "operation",
)
_NUMERIC_NAME_HINTS = ("pr", "rate", "fraction", "number", "normalization", "signal strength")


def _classify_point(name: str, unit: str, point_id: str) -> _ClassPair:
    """Build-time classification for a catalog row."""
    if point_id in ENUM_MAPS:
        return (SensorDeviceClass.ENUM, None)
    by_unit = _classify_by_unit(unit)
    if by_unit is not None:
        return by_unit
    lowered = name.lower()
    # Health first so "Battery Health (SOH)" is never mistaken for charge level.
    if "soh" in lowered or "health" in lowered:
        return (None, _MEASUREMENT)
    is_percent = unit.strip() in ("%", "percent")
    is_charge = "soc" in lowered or "battery level" in lowered or "state of charge" in lowered
    if is_percent or is_charge:
        return (SensorDeviceClass.BATTERY, _MEASUREMENT)
    if "power factor" in lowered or lowered.strip() == "pf":
        return (SensorDeviceClass.POWER_FACTOR, _MEASUREMENT)
    if any(h in lowered for h in _NUMERIC_NAME_HINTS):
        return (None, _MEASUREMENT)
    if any(h in lowered for h in _TEXT_NAME_HINTS):
        return (None, None)
    return (None, None)


def _build_catalog() -> dict[str, PointInfo]:
    catalog: dict[str, PointInfo] = {}
    for point_id, name, unit in RAW_POINTS:
        device_class, state_class = _classify_point(name, unit, point_id)
        options = resolve_enum_options(point_id)
        catalog[point_id] = PointInfo(name, device_class, state_class, options)
    return catalog


POINT_CATALOG: dict[str, PointInfo] = _build_catalog()  # replaces the Task 3 stub
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: PASS (all integrity + fallback cases green).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/measure_points_data.py custom_components/sungrow/measure_points.py tests/test_measure_points.py
git commit -m "feat: transcribe comprehensive measure-point catalog (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Code aliases + `resolve_name`

**Files:**
- Modify: `custom_components/sungrow/measure_points_data.py` (`CODE_ALIASES`)
- Modify: `custom_components/sungrow/measure_points.py` (`resolve_name`)
- Modify: `tests/test_measure_points.py`

**Interfaces:**
- Produces (data): `CODE_ALIASES: dict[str, str]` — clean names for pysolarcloud built-in codes and the recommended user codes from `docs/SENSORS.md`.
- Produces: `resolve_name(point_id: str, code: str, api_name: str | None) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_measure_points.py
def test_resolve_name_alias_wins():
    assert mp.resolve_name("83240", "total_field_power_factor", "PF-cn") == "Battery Power Factor"


def test_resolve_name_numeric_code_uses_catalog():
    # Opaque numeric code -> English catalog name, not the (often Chinese) API name.
    assert mp.resolve_name("58601", "58601", "电池电压") == "Battery Voltage"


def test_resolve_name_readable_code_title_cases():
    assert mp.resolve_name("0", "total_active_power", None) == "Total Active Power"


def test_resolve_name_unknown_numeric_falls_back():
    assert mp.resolve_name("99999", "99999", None) == "Sensor 99999"
    assert mp.resolve_name("99999", "99999", "Some API Name") == "Some API Name"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: FAIL — `AttributeError: ... 'resolve_name'`.

- [ ] **Step 3: Write minimal implementation**

Populate `CODE_ALIASES` in `measure_points_data.py` (migrate the existing `SENSOR_ALIASES` + `EXTRA_CODE_ALIASES` from `sensor.py`, add the cleaned built-ins):

```python
# measure_points_data.py — replace the empty CODE_ALIASES
CODE_ALIASES: dict[str, str] = {
    # Built-in plant/battery default codes (from pysolarcloud).
    "total_field_energy_storage_active_power": "Battery Power",
    "total_field_energy_storage_maximum_reactive_power": "Battery Max Reactive Power",
    "total_field_chargeable_energy": "Battery Chargeable Energy",
    "total_field_dischargeable_energy": "Battery Dischargeable Energy",
    "total_field_maximum_rechargeable_power": "Battery Max Charge Power",
    "total_field_maximum_dischargeable_power": "Battery Max Discharge Power",
    "total_field_power_factor": "Battery Power Factor",
    "total_field_reactive_power": "Battery Reactive Power",
    "total_field_soc": "Battery State of Charge (Field)",
    "daily_field_charge_capacity": "Battery Daily Charge Capacity",
    "daily_field_discharge_capacity": "Battery Daily Discharge Capacity",
    "total_field_charge_capacity": "Battery Total Charge Capacity",
    "total_field_discharge_capacity": "Battery Total Discharge Capacity",
    "total_number_of_charge_discharge": "Battery Charge/Discharge Cycles",
    "energy_storage_active_power_ems": "EMS Battery Power",
    "energy_storage_soc_ems": "EMS Battery SOC",
    "battery_level_soc": "Battery State of Charge",
    "meter_pr": "Meter Performance Ratio",
    "plant_pr": "Plant Performance Ratio",
    "inverter_pr": "Inverter Performance Ratio",
    "power_fraction": "Plant Power / Installed Power",
    # Recommended user-supplied codes (docs/SENSORS.md).
    "battery_charge_power": "Battery Charge Power",
    "battery_discharge_power": "Battery Discharge Power",
    "ev_charger_power": "EV Charger Power",
    "ev_charger_energy": "EV Charger Energy",
    "battery_level": "Battery Level",
    "battery_soh": "Battery Health (SOH)",
    "battery_voltage": "Battery Voltage",
    "battery_current": "Battery Current",
    "battery_temperature": "Battery Temperature",
    "battery_total_charge_energy": "Battery Total Charge Energy",
    "battery_total_discharge_energy": "Battery Total Discharge Energy",
    "ev_charger_max_power": "EV Charger Max Power",
    "ev_charger_status": "EV Charger Status",
    "meter_forward_active_energy": "Meter Forward Active Energy",
    "meter_reverse_active_energy": "Meter Reverse Active Energy",
    "meter_daily_forward_active_energy": "Meter Daily Forward Active Energy",
    "meter_daily_reverse_active_energy": "Meter Daily Reverse Active Energy",
    "meter_active_power": "Meter Active Power",
    "meter_power_factor": "Meter Power Factor",
}
```

```python
# measure_points.py
def resolve_name(point_id: str, code: str, api_name: str | None) -> str:
    """Resolve the display name for a point.

    Precedence: curated alias -> catalog English name for opaque numeric codes
    -> title-cased readable code -> API name -> ``Sensor {id}``.
    """
    alias = CODE_ALIASES.get(code)
    if alias:
        return alias
    if code.isdigit():
        info = POINT_CATALOG.get(point_id)
        if info is not None:
            return info.name
        return api_name or f"Sensor {point_id}"
    return code.replace("_", " ").title()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_measure_points.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/measure_points_data.py custom_components/sungrow/measure_points.py tests/test_measure_points.py
git commit -m "feat: add code aliases and name resolver (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Wire resolvers into `sensor.py`

**Files:**
- Modify: `custom_components/sungrow/sensor.py`
- Modify: `tests/test_sensor.py`

**Interfaces:**
- Consumes: `resolve_name`, `resolve_classification`, `resolve_enum_value` from `measure_points`.
- Keeps: `infer_device_class(unit, code, point_id)` as a public thin wrapper delegating to `resolve_classification` (existing import site in tests).

- [ ] **Step 1: Update the failing tests**

Update `tests/test_sensor.py`:
- Change every `infer_device_class(unit, code)` call to `infer_device_class(unit, code, "")` (unknown point ID).
- Change the SOH-style expectation if present; add the new cases below.

```python
# add to tests/test_sensor.py
def test_infer_device_class_power_factor_no_unit():
    from custom_components.sungrow.sensor import infer_device_class
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    assert infer_device_class("", "meter_power_factor", "0") == (
        SensorDeviceClass.POWER_FACTOR,
        SensorStateClass.MEASUREMENT,
    )


def test_native_value_enum_maps_label(hass):
    """An enum point returns its human label, not the raw code."""
    from custom_components.sungrow.sensor import SungrowSensor

    coordinator = _make_coordinator(hass)  # existing helper / fixture
    data = {"id": "33716", "code": "ev_charger_status", "value": 3, "unit": ""}
    coordinator.data = {"ev_charger_status": data}
    sensor = SungrowSensor(coordinator, "ev_charger_status", "P1", "Plant", data)
    assert sensor.device_class == SensorDeviceClass.ENUM
    assert sensor.native_value == "Charging"


def test_native_value_unitless_numeric_is_float(hass):
    """A dimensionless power-factor value now coerces to float (was text)."""
    from custom_components.sungrow.sensor import SungrowSensor

    coordinator = _make_coordinator(hass)
    data = {"id": "8014", "code": "meter_power_factor", "value": "0.98", "unit": ""}
    coordinator.data = {"meter_power_factor": data}
    sensor = SungrowSensor(coordinator, "meter_power_factor", "P1", "Plant", data)
    assert sensor.native_value == 0.98
```

(Reuse the existing coordinator fixture/helper in `tests/test_sensor.py`; if there is no `_make_coordinator`, build the `MagicMock` coordinator the same way the current `SungrowSensor` tests do.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sensor.py -q`
Expected: FAIL — old `infer_device_class` arity / no enum handling.

- [ ] **Step 3: Implement the wiring**

In `sensor.py`:
1. Remove `SENSOR_ALIASES`, `EXTRA_CODE_ALIASES`, `_UNIT_CLASS_MAP`, `_PERCENT_BATTERY_HINTS`, and the now-unused module constants `MEASUREMENT`/`TOTAL_INCREASING` (they moved to `measure_points.py`). Keep the `SensorDeviceClass`/`SensorStateClass` imports (still used for the ENUM branch).
2. Add imports and the wrapper:

```python
from .measure_points import (
    resolve_classification,
    resolve_enum_value,
    resolve_name,
)


def infer_device_class(
    unit: str | None, point_code: str, point_id: str = ""
) -> tuple[SensorDeviceClass | None, SensorStateClass | None]:
    """Public wrapper kept for tests; delegates to the resolver."""
    return resolve_classification(unit, point_code, point_id)
```

3. Rewrite `_apply_point_metadata` to use the resolvers and carry the point ID:

```python
def _apply_point_metadata(self, point_code: str, init_data: dict[str, Any], label: str) -> None:
    point_id = str(init_data.get("id") or point_code)
    self._point_id = point_id

    self._attr_name = resolve_name(point_id, point_code, init_data.get("name"))
    _LOGGER.debug("Created sensor: %s %s (code: %s)", label, self._attr_name, point_code)

    initial_value = init_data.get("value")
    if initial_value is None or str(initial_value).strip() == "" or str(initial_value).lower() == "unknown":
        self._attr_entity_registry_enabled_default = False

    unit = init_data.get("unit")
    device_class, state_class = resolve_classification(unit, point_code, point_id)
    self._attr_device_class = device_class
    self._attr_state_class = state_class

    if device_class == SensorDeviceClass.ENUM:
        self._attr_options = list(resolve_enum_options(point_id) or [])
        self._attr_native_unit_of_measurement = None
    else:
        self._attr_native_unit_of_measurement = unit if unit else None

    self._attr_icon = None if device_class else "mdi:solar-power-variant"
```

4. Add an enum branch to `native_value` (before the numeric coercion):

```python
@property
def native_value(self) -> float | str | None:
    point = self._current_point()
    if point is None:
        return None
    val: Any = point.get("value")
    if val is None:
        return None
    if self._attr_device_class == SensorDeviceClass.ENUM:
        return resolve_enum_value(self._point_id, val)
    if self._attr_device_class is None and self._attr_state_class is None:
        return str(val)
    try:
        return float(val)
    except (ValueError, TypeError):
        return cast("str | None", val)
```

5. `SungrowDeviceSensor.__init__` already calls `_apply_point_metadata`; ensure `self._point_id` is set there too (it is, via the shared helper). Add `self._point_id: str = point_code` default in `SungrowSensor.__init__` before `_apply_point_metadata` for safety.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sensor.py tests/test_measure_points.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sungrow/sensor.py tests/test_sensor.py
git commit -m "feat: classify and name sensors via the measure-point catalog (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Regenerate `docs/SENSORS.md`

**Files:**
- Modify: `docs/SENSORS.md`

- [ ] **Step 1: Update the docs**

Extend the **Recommended measure points** section with comprehensive per-device-type `point_id=code` blocks (battery, charger, energy-meter, energy-storage-inverter, EMS, plant-field, combiner-box, PCS, microinverter, environment), each linking to its mcp-isolarcloud catalog. Add a sentence: "Device and state classes are inferred automatically — the SOC, SOH, power-factor, PR and cycle-count points now classify correctly even though the API reports no unit, and status points (EV charger, inverter operating state) show human-readable text." Keep the existing dispatch/EV sections. Ensure any code named here exists as a key in `CODE_ALIASES` (so the friendly name shows).

- [ ] **Step 2: Verify no broken references**

Run: `.venv/bin/python -m pytest tests/test_strings.py -q` (unaffected, sanity) and manually confirm every `code` in the new doc blocks is present in `measure_points_data.CODE_ALIASES`.

- [ ] **Step 3: Commit**

```bash
git add docs/SENSORS.md
git commit -m "docs: comprehensive measure-point catalog in SENSORS.md (#105)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Full CI gate + PR

**Files:** none (verification + PR).

- [ ] **Step 1: Lint, format, type-check, test (mirror CI)**

```bash
.venv/bin/ruff check custom_components/ tests/
.venv/bin/ruff format --check custom_components/ tests/
.venv/bin/mypy
.venv/bin/python -m pytest tests/
```
Expected: all green; coverage ≥ `fail_under`. Fix any ruff/mypy findings (e.g. add `from __future__ import annotations`, type the data lists) and re-run.

- [ ] **Step 2: Sanity-import check**

```bash
.venv/bin/python -c "from custom_components.sungrow import measure_points as m; print(len(m.POINT_CATALOG), 'points')"
```
Expected: prints a count ≥ 300.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/comprehensive-measure-point-mapping
gh pr create --title "feat: comprehensive measure-point mapping + classification (#105)" --body "$(cat <<'EOF'
Resolves #105.

Surfaces every documented iSolarCloud measure point with a friendly English name and a correct device/state class, grounded in the mcp-isolarcloud catalogs.

## What
- New `measure_points.py` / `measure_points_data.py`: a point-ID-keyed catalog (~all documented points across 17 device types), documented status-enum tables, and pure resolver functions.
- Fix: dimensionless numerics (SOC, SOH, power factor, PR, cycle counts) now classify as numeric/battery/power-factor instead of degrading to **text** sensors that never graph.
- Fix: `battery_soh` is no longer mislabelled `BATTERY` (health ≠ charge level).
- EV charger / inverter operating status now render as human-readable ENUM states.
- Extended unit map (%RH, m/s, hPa, mm, W/m², h, varh, kΩ…) for the environment/meter catalogs.
- `docs/SENSORS.md` regenerated with comprehensive copy-paste `point_id=code` blocks.

## Migration
Entity IDs (from `unique_id = {plant_id}_{code}`) are unchanged. Some existing default entities change device/state class (e.g. `total_field_power_factor` text→POWER_FACTOR numeric; unitless SOC→BATTERY) and a few display names get cleaner.

## Tests
New `tests/test_measure_points.py` (classifiers, resolvers, enum, catalog integrity) + updated `tests/test_sensor.py` (enum + unitless-numeric `native_value`, naming precedence). ruff + mypy + pytest green.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Confirm CI checks pass** (`lint`, `test`, `hacs_validate`) on the PR.

---

## Self-review notes

- **Spec coverage:** new module ✅ (T1–T5); comprehensive catalog ✅ (T4); classification layer incl. SOH fix & power-factor & unitless-numeric ✅ (T3/T4); enum translation for the 4 documented points ✅ (T2/T6); extended unit map ✅ (T1); `sensor.py` wiring + enum `native_value` ✅ (T6); `docs/SENSORS.md` ✅ (T7); tests + migration note ✅ (T6/T8).
- **Naming precedence** (spec) implemented in `resolve_name` (T5): alias → catalog-by-ID-for-numeric-code → title-case → API name.
- **API-unit-authoritative** invariant enforced by ordering in `resolve_classification` (T3): unit map runs before catalog/keyword fallbacks.
- **Type consistency:** `_ClassPair`, `PointInfo`, `resolve_classification(unit, code, point_id)`, `resolve_name(point_id, code, api_name)`, `resolve_enum_value(point_id, value)` used consistently across tasks and in `sensor.py`.
- **Enum integrity:** `test_enum_maps_reference_real_catalog_points` guards that every `ENUM_MAPS` key is a real catalog point with `options`.
