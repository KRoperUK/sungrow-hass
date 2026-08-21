"""Map the user-account ``getPsDetail`` payload onto the measure-point model (#269).

The unofficial app/web API returns a flat plant-detail dict rather than the OAuth
realtime shape. The measure points arrive as ``p<ID>_map`` / ``p<ID>_map_virgin`` pairs
(the ``_virgin`` variant carries the raw value in its base unit, e.g. ``Wh``) keyed by the
same measure-point IDs the integration's catalog already knows, plus a handful of named
plant-level fields (``curr_power`` etc). This module turns that into the integration's
``{code: {id, code, value, unit}}`` realtime shape. Measure points use the *bare numeric*
point ID as their code so ``resolve_name`` / ``resolve_classification`` take the digit-code
catalog path and produce the same English names/entity slugs the OAuth transport gives
(#269). Energy-unit normalisation is applied by the caller.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Codes already reported as unitless, so the warning below fires once per code per
# process rather than on every poll.
_WARNED_MISSING_UNIT: set[str] = set()

# ``p83106_map_virgin`` -> point id 83106 (raw value in its base unit — preferred).
_VIRGIN_RE = re.compile(r"^p(\d+)_map_virgin$")
# ``p83106_map`` -> point id 83106 (display value — fallback when no _virgin exists).
_MAP_RE = re.compile(r"^p(\d+)_map$")

# Named plant-level dict fields ({value, unit}) exposed with a stable code. These are
# not catalog point IDs, so the code doubles as the naming/classification key.
_NAMED_DICT_FIELDS: dict[str, str] = {
    "curr_power": "current_power",
    "month_energy": "month_energy",
    "today_energy": "today_energy",
    "total_energy": "total_energy",
    "co2_reduce_total": "co2_reduce_total",
    "today_income": "today_income",
    "total_income": "total_income",
}
# Named scalar diagnostic fields exposed as plain (unitless) sensors.
_NAMED_SCALAR_FIELDS: dict[str, str] = {
    "alarm_count": "alarm_count",
    "fault_count": "fault_count",
}


def map_plant_detail_to_points(detail: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``getPsDetail`` ``result_data`` dict to the realtime point shape.

    Prefers the raw ``p<ID>_map_virgin`` value (explicit base unit) over the display
    ``p<ID>_map``; skips empty values. Returns ``{code: {id, code, value, unit}}``.
    """
    raw: dict[str, tuple[Any, Any]] = {}
    # Pass 1: raw (virgin) values win — they carry the real base unit.
    for key, val in detail.items():
        if not isinstance(val, dict):
            continue
        m = _VIRGIN_RE.match(key)
        if m:
            raw[m.group(1)] = (val.get("value"), val.get("unit"))
    # Pass 2: display values only for point IDs without a virgin variant.
    for key, val in detail.items():
        if not isinstance(val, dict):
            continue
        m = _MAP_RE.match(key)
        if m and m.group(1) not in raw:
            raw[m.group(1)] = (val.get("value"), val.get("unit"))

    points: dict[str, Any] = {}
    for point_id, (value, unit) in raw.items():
        if value in (None, ""):
            continue
        # Use the bare numeric point ID as the code so the resolver's digit-code path
        # (``resolve_name``) consults the measure-point catalog and produces the same
        # English name/entity slug the OAuth transport gives (#269). A ``p<ID>`` code is
        # non-numeric and would fall back to an opaque "P<ID>" title-case name.
        points[point_id] = {"id": point_id, "code": point_id, "value": value, "unit": unit or ""}

    for field, code in _NAMED_DICT_FIELDS.items():
        val = detail.get(field)
        if isinstance(val, dict) and val.get("value") not in (None, ""):
            unit = val.get("unit") or ""
            if not unit and code not in _WARNED_MISSING_UNIT:
                # Without a unit the value's scale is unknowable (this field is observed
                # arriving as both W and kW), so the sensor gets a state class but no
                # device class or unit. Surface it once so the real payload can be
                # captured and the base unit pinned down (#384).
                _WARNED_MISSING_UNIT.add(code)
                _LOGGER.warning(
                    "iSolarCloud returned no unit for plant field %r (value=%r); the %r "
                    "sensor will record statistics but without a unit or device class. "
                    "Please report this payload at "
                    "https://github.com/KRoperUK/sungrow-hass/issues/384",
                    field,
                    val.get("value"),
                    code,
                )
            points[code] = {"id": code, "code": code, "value": val.get("value"), "unit": unit}

    for field, code in _NAMED_SCALAR_FIELDS.items():
        val = detail.get(field)
        if val is not None and val != "":
            points[code] = {"id": code, "code": code, "value": val, "unit": ""}

    return points


# Placeholders the app/web API uses for "this device does not report this point".
_EMPTY_VALUES = frozenset({"", "--", "-", "null", "none", "unknown"})


def map_device_list_to_points(devices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Extract each device's embedded ``point_data`` into the per-device realtime shape.

    Unlike the OAuth transport — where the device list is slow-changing metadata and
    realtime values come from a separate per-device endpoint — the app/web device-list
    response embeds every device's *current* readings as a ``point_data`` array:

    .. code-block:: python

        {"device_type": 43, "uuid": ..., "point_data": [
            {"point_id": 58604, "point_name": "Battery SOC", "unit": "%", "value": "32.2"},
        ]}

    So this list is both the device inventory and the per-device data source (#389).
    Returns ``{device_uuid: {code: {id, code, value, unit, name}}}``.

    As in :func:`map_plant_detail_to_points`, the *bare numeric* point ID becomes the
    code so ``resolve_name`` / ``resolve_classification`` take the catalog path and
    produce the same English names and classes the OAuth transport gives. The display
    ``value``/``unit`` pair is used rather than ``raw_value``, which is in the point's
    base unit but carries no unit field to interpret it with.
    """
    out: dict[str, dict[str, Any]] = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        uuid = device.get("uuid")
        raw_points = device.get("point_data")
        if uuid is None or not isinstance(raw_points, list):
            continue
        points: dict[str, Any] = {}
        for entry in raw_points:
            if not isinstance(entry, dict):
                continue
            point_id = entry.get("point_id")
            if point_id is None:
                continue
            value = entry.get("value")
            if value is None or str(value).strip().lower() in _EMPTY_VALUES:
                continue
            code = str(point_id)
            points[code] = {
                "id": code,
                "code": code,
                "value": value,
                "unit": entry.get("unit") or "",
                # Only used if the catalog has no row for this ID.
                "name": entry.get("point_name") or None,
            }
        if points:
            out[str(uuid)] = points
    return out
