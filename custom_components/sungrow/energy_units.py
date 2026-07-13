"""Normalize iSolarCloud energy units for consistent HA Energy dashboard use.

Plant-level points often arrive in Wh while inverter points arrive in kWh for the
same physical quantity (e.g. total_yield 6_467_800 Wh vs 6467.8 kWh). Converting
Wh → kWh keeps related entities comparable and avoids mixed-unit Energy config.
"""

from __future__ import annotations

from typing import Any

_WH_UNITS = frozenset({"wh", "w·h", "w.h", "watt-hour", "watt hour", "watthour"})


def normalize_energy_point(point: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *point* with Wh values scaled to kWh when applicable."""
    unit = str(point.get("unit") or "").strip()
    if unit.lower() not in _WH_UNITS:
        return point
    raw = point.get("value")
    if raw is None or raw == "":
        return {**point, "unit": "kWh"}
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return {**point, "unit": "kWh"}
    return {**point, "value": round(num / 1000.0, 3), "unit": "kWh"}


def normalize_energy_units(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize every point dict in a realtime payload (plant or per-device)."""
    out: dict[str, Any] = {}
    for code, point in data.items():
        if isinstance(point, dict):
            out[code] = normalize_energy_point(point)
        else:
            out[code] = point
    return out


def tag_source(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Ensure every point dict carries a ``source`` provenance tag."""
    out: dict[str, Any] = {}
    for code, point in data.items():
        if isinstance(point, dict) and point.get("source") is None:
            out[code] = {**point, "source": source}
        else:
            out[code] = point
    return out
