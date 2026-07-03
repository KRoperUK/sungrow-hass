"""Diagnostics support for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import enum
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from . import SungrowConfigEntry, SungrowData

_LOGGER = logging.getLogger(__name__)


def _jsonable(obj: Any) -> Any:
    """Make API payloads JSON-serialisable for the diagnostics download.

    pysolarcloud converts *known* device types / fault statuses to ``enum`` members
    (which ``json`` can't serialise); an *unknown* device type — e.g. an EV charger
    the library hasn't catalogued — is left as its raw int, which is exactly the
    identifier we want to surface. Convert enums to ``"NAME (value)"`` and recurse
    into containers; everything else passes through untouched.
    """
    if isinstance(obj, enum.Enum):
        return f"{obj.name} ({obj.value})"
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


async def _probe_plant_devices(service: Any, plant_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Best-effort capture of every device and its per-device realtime data.

    Returns ``(all_devices, device_realtime)``. The plant realtime endpoint only
    returns point IDs the library knows about, so hardware like EV chargers never
    shows up as sensors (issue #18). This walks the *full* device list (not just
    inverter/ESS) and attempts a per-device realtime fetch for each distinct type,
    so a diagnostics download reveals an unmapped device's type and reachable
    points — enough to add a proper mapping. All failures are captured, never
    raised, so a diagnostics download always succeeds.
    """
    try:
        all_devices = await service.async_get_plant_devices(plant_id)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Diagnostics: could not list devices for plant %s: %s", plant_id, err)
        return [{"error": str(err)}], {}

    device_realtime: dict[str, Any] = {}
    seen_types: set[Any] = set()
    for device in all_devices:
        if not isinstance(device, dict):
            continue
        device_type = device.get("device_type")
        if device_type is None:
            continue
        type_id = getattr(device_type, "value", device_type)
        if type_id in seen_types:
            continue
        seen_types.add(type_id)
        try:
            device_realtime[str(type_id)] = _jsonable(await service.async_get_device_realtime(plant_id, device_type))
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Diagnostics: device realtime failed for type %s: %s", type_id, err)
            device_realtime[str(type_id)] = {"error": str(err)}

    return _jsonable(all_devices), device_realtime


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SungrowConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Tokens and secrets are redacted; raw coordinator data, the full device list,
    and per-device realtime data are included to help identify unsupported point
    IDs (e.g. EV chargers — see issue #18).
    """
    data = getattr(entry, "runtime_data", None)
    coordinators = data.coordinators if isinstance(data, SungrowData) else []
    devices_by_plant = data.devices if isinstance(data, SungrowData) else {}

    plant_data: dict[str, dict[str, Any]] = {}
    for coordinator in coordinators:
        plant_id = coordinator.plant_id
        service = getattr(coordinator, "plants_service", None)
        all_devices: list[dict[str, Any]] = []
        device_realtime: dict[str, Any] = {}
        if service is not None:
            all_devices, device_realtime = await _probe_plant_devices(service, plant_id)

        plant_data[plant_id] = {
            "plant_name": coordinator.plant_name,
            "last_update_success": coordinator.last_update_success,
            "data": coordinator.data,
            # Devices the integration created dispatch entities for (inverter/ESS).
            "devices": _jsonable(devices_by_plant.get(plant_id, [])),
            # Every device on the plant, including hardware without mapped sensors.
            "all_devices": all_devices,
            # Per-device-type realtime data (best effort; {} where unsupported).
            "device_realtime": device_realtime,
        }

    return {
        "entry_id": entry.entry_id,
        "gateway": entry.data.get("gateway"),
        "app_id": entry.data.get("app_id"),
        "tokens_present": "tokens" in entry.data,
        "options": dict(entry.options),
        "plants": plant_data,
    }
