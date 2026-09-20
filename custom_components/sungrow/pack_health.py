"""Derived battery pack-health points from the cell/module extremes (#430).

The iSolarCloud battery device reports the *extremes* — highest and lowest cell
voltage, highest and lowest module temperature — as separate points. The useful
``diagnostic`` is the spread between them: a widening cell-voltage spread is the
usual early sign of a weak or failing cell, and a widening module-temperature
spread points at a thermal problem, neither of which is visible from the raw
max/min pair on a dashboard.

Both derived points are computed from data the device sensors already fetch, so
this costs no extra API calls. Only the arithmetic happens here; naming, units and
classification stay with the normal measurement-point machinery (``mV`` classifies
as voltage, ``°C`` as temperature).
"""

from __future__ import annotations

from typing import Any

# (derived code, max source code, min source code, unit) triples. The spread is
# ``max - min``; the unit comes from the sources, both of which report in it.
_SPREADS: tuple[tuple[str, str, str, str], ...] = (
    ("cell_imbalance", "battery_max_cell_voltage", "battery_min_cell_voltage", "mV"),
    ("module_temperature_spread", "battery_max_module_temperature", "battery_min_module_temperature", "°C"),
)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def add_pack_health_points(points: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return ``points`` plus the derived battery pack-health spreads.

    A spread whose max/min pair is missing, non-numeric or inverted is skipped: a
    negative spread cannot happen physically, so it means one of the two points is
    wrong and publishing the arithmetic on top of it would only add noise. Devices
    that report no cell/module detail (string inverters, meters, most batteries)
    are returned untouched, so this is safe to run over every device payload.

    ``source`` keeps the transport that supplied the pair and marks the value as
    derived, matching the ``modbus_derived`` convention from the local daily energy
    derivation (#471).
    """
    out = dict(points)
    for code, max_code, min_code, unit in _SPREADS:
        high_point = points.get(max_code)
        low_point = points.get(min_code)
        if not isinstance(high_point, dict) or not isinstance(low_point, dict):
            continue
        high = _as_float(high_point.get("value"))
        low = _as_float(low_point.get("value"))
        if high is None or low is None or high < low:
            continue
        source = str(high_point.get("source") or "cloud")
        out[code] = {
            "code": code,
            "value": round(high - low, 3),
            "unit": unit,
            "source": f"{source}_derived",
        }
    return out
