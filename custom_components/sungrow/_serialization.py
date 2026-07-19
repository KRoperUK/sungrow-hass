"""JSON-safe serialisation + anonymisation helpers shared across the integration.

Extracted from ``diagnostics.py`` (#355) so future features that need to
represent API payloads as JSON — a "debug snapshot" service, a point-catalog
UI, log-friendly formatters — can reuse the same primitives instead of
duplicating them or reaching into ``diagnostics``' private surface.

Three low-level primitives are exposed here:

* :func:`jsonable` — convert enums (``DeviceType``, ``DeviceFaultStaus``) to
  ``"NAME (value)"`` strings so :mod:`json` can serialise them, recursing
  through dicts, lists, and tuples.

* :func:`anonymise_device_keys` — replace device-uuid keys in a
  ``{uuid: payload}`` dict with stable ``device_N`` placeholders. HA's
  ``async_redact_data`` scrubs values under known key names but can't reach
  keys themselves, so uuids leaked to diagnostics bundles before this.

* :func:`catalog_rows` — flatten a ``{code: point}`` realtime response into
  sorted ``{point_id, code, name, value, unit}`` rows suitable for a
  human-readable point catalog.

The higher-level ``build_points_catalog`` (which knows about
``model_capabilities``) stays in ``diagnostics.py`` because it's specific to
the diagnostics bundle shape.
"""

from __future__ import annotations

import enum
from typing import Any

from .measure_points import resolve_name


def jsonable(obj: Any) -> Any:
    """Return a JSON-serialisable copy of *obj*, converting enums to strings.

    pysolarcloud converts known device types and fault statuses to
    :class:`~enum.Enum` members (``json`` can't serialise those directly). An
    *unknown* device type — e.g. an EV charger the library hasn't catalogued
    — is left as its raw int, which is exactly the identifier we want to
    surface in a diagnostics dump. This helper converts enums to
    ``"NAME (value)"`` strings and recurses through dicts, lists, and tuples;
    everything else passes through untouched.
    """
    if isinstance(obj, enum.Enum):
        return f"{obj.name} ({obj.value})"
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def anonymise_device_keys(realtime: Any, uuid_map: dict[str, str]) -> Any:
    """Replace device-uuid keys in a per-device realtime dict with stable placeholders.

    ``Plants.async_get_device_realtime`` returns ``{device_uuid: {...points...}}``
    — the device uuids are dict *keys*, not values named ``uuid``, so HA's
    ``async_redact_data`` (which only scrubs values under known key names)
    can't reach them and they would otherwise leak into a diagnostics
    download (#122). Map each uuid to a stable ``device_N`` placeholder,
    sharing ``uuid_map`` across device types within a plant so the same
    device keeps the same label. Non-dict payloads (e.g. an
    ``{"error": ...}`` capture) pass through untouched — their keys are not
    uuids.
    """
    if not isinstance(realtime, dict):
        return realtime
    anonymised: dict[str, Any] = {}
    for uuid, payload in realtime.items():
        placeholder = uuid_map.setdefault(str(uuid), f"device_{len(uuid_map) + 1}")
        anonymised[placeholder] = payload
    return anonymised


def catalog_rows(points: Any) -> list[dict[str, Any]]:
    """Flatten a ``{code: point}`` realtime dict into tidy, sorted catalog rows.

    Each row is ``{point_id, code, name, value, unit}`` — the exact fields a
    user needs to pick a point for the "Extra measure points" option (#252).
    The friendly English ``name`` comes from the measure-point catalog
    (:func:`~custom_components.sungrow.measure_points.resolve_name`), not the
    API's Chinese-for-English-locale name nor the user's device name, so the
    rows carry no PII.
    """
    if not isinstance(points, dict):
        return []
    rows: list[dict[str, Any]] = []
    for code, point in points.items():
        if not isinstance(point, dict):
            continue
        point_id = str(point.get("id") or code)
        rows.append(
            {
                "point_id": point_id,
                "code": str(code),
                "name": resolve_name(point_id, str(code), point.get("name")),
                "value": point.get("value"),
                "unit": point.get("unit"),
            }
        )
    return sorted(rows, key=lambda row: row["point_id"])
