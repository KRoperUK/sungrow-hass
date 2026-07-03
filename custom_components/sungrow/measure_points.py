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

from .measure_points_data import CODE_ALIASES, ENUM_MAPS, RAW_POINTS

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


# Percentage / dimensionless codes that mean charge level (battery device class)
# vs. health (numeric only) vs. power factor.
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


# --- Build-time catalog classification (from documented name + unit) ----------

# Categorical points return string states (statuses, versions, serials, IDs) and
# must stay text even if a numeric keyword also appears in the name.
_TEXT_NAME_HINTS = ("status", "mode", "version", "s/n", "serial", "card id")
# Dimensionless-but-numeric name tokens (checked as whole words to avoid e.g.
# "pr" matching "purchased").
_NUMERIC_NAME_TOKENS = frozenset({"pr", "rate", "ratio", "number", "normalization", "fraction"})


def _name_tokens(name: str) -> set[str]:
    cleaned = name.lower()
    for ch in "/-().,#":
        cleaned = cleaned.replace(ch, " ")
    return set(cleaned.split())


def _classify_point(name: str, unit: str, point_id: str) -> _ClassPair:
    """Classify a catalog row from its documented name and unit (build time)."""
    if point_id in ENUM_MAPS:
        return (SensorDeviceClass.ENUM, None)
    by_unit = _classify_by_unit(unit)
    if by_unit is not None:
        return by_unit

    lowered = name.lower()
    tokens = _name_tokens(name)
    # Text/categorical points first so "Version Number" isn't read as numeric.
    if any(h in lowered for h in _TEXT_NAME_HINTS):
        return (None, None)
    if "soh" in lowered or "health" in lowered:
        return (None, _MEASUREMENT)
    if "soc" in tokens or "battery level" in lowered or "state of charge" in lowered:
        return (SensorDeviceClass.BATTERY, _MEASUREMENT)
    if "power factor" in lowered:
        return (SensorDeviceClass.POWER_FACTOR, _MEASUREMENT)
    if tokens & _NUMERIC_NAME_TOKENS or "signal strength" in lowered:
        return (None, _MEASUREMENT)
    return (None, None)


def _build_catalog() -> dict[str, PointInfo]:
    catalog: dict[str, PointInfo] = {}
    for point_id, name, unit in RAW_POINTS:
        device_class, state_class = _classify_point(name, unit, point_id)
        catalog[point_id] = PointInfo(name, device_class, state_class, resolve_enum_options(point_id))
    return catalog


POINT_CATALOG: dict[str, PointInfo] = _build_catalog()
