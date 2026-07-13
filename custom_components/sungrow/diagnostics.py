"""Diagnostics support for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SungrowConfigEntry, SungrowData

_LOGGER = logging.getLogger(__name__)

# Bound each best-effort probe request so a hung endpoint can't stall (or pile up
# calls against the quota during) a diagnostics download.
PROBE_TIMEOUT = 30

# Identifiers redacted from the diagnostics download. Credentials
# (app_key/app_secret) and tokens are never included in the payload to begin
# with; this additionally scrubs the App ID and the per-device hardware
# identifiers (uuid, ps_key, and every serial-number field — including the
# communication dongle's ``communication_dev_sn``).
#
# ``plant_name`` / ``device_name`` / ``ps_name`` are redacted too: users routinely
# name their plant and inverter after their address or location (e.g. "7 Acacia
# Avenue" / "Acacia-Avenue-Inverter"), so leaving them in a bundle a user shares for
# support would leak a home address. The hardware is still identifiable for support
# from ``device_type`` / ``device_model_code`` / ``factory_name`` (kept), and reports
# stay correlatable via ``ps_id`` (the plant id — deliberately kept; not a secret).
TO_REDACT = {
    "app_id",
    "uuid",
    "ps_key",
    "dev_sn",
    "sn",
    "device_sn",
    "communication_dev_sn",
    "plant_name",
    "device_name",
    "ps_name",
}


def _anonymise_device_keys(realtime: Any, uuid_map: dict[str, str]) -> Any:
    """Replace device-uuid keys in a per-device realtime dict with stable placeholders.

    ``async_get_device_realtime`` returns ``{device_uuid: {...points...}}`` — the device
    uuids are dict *keys*, not values named ``uuid``, so ``async_redact_data`` (which only
    scrubs values under known key names) can't reach them and they would otherwise leak
    into a diagnostics download (#122). Map each uuid to a stable ``device_N`` placeholder,
    sharing ``uuid_map`` across device types within a plant so the same device keeps the
    same label. Non-dict payloads (e.g. an ``{"error": ...}`` capture) pass through
    untouched — their keys are not uuids.
    """
    if not isinstance(realtime, dict):
        return realtime
    anonymised: dict[str, Any] = {}
    for uuid, payload in realtime.items():
        placeholder = uuid_map.setdefault(str(uuid), f"device_{len(uuid_map) + 1}")
        anonymised[placeholder] = payload
    return anonymised


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
        async with asyncio.timeout(PROBE_TIMEOUT):
            all_devices = await service.async_get_plant_devices(plant_id)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Diagnostics: could not list devices for plant %s: %s", plant_id, err)
        return [{"error": str(err)}], {}

    device_realtime: dict[str, Any] = {}
    seen_types: set[Any] = set()
    # Shared across device types so a given device uuid always maps to the same
    # ``device_N`` placeholder within this plant's per-device realtime section (#122).
    uuid_map: dict[str, str] = {}
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
            async with asyncio.timeout(PROBE_TIMEOUT):
                realtime = await service.async_get_device_realtime(plant_id, device_type)
            device_realtime[str(type_id)] = _anonymise_device_keys(_jsonable(realtime), uuid_map)
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

        modbus_diag: dict[str, Any] = {}
        if getattr(coordinator, "modbus_diagnostics", None):
            modbus_diag = dict(coordinator.modbus_diagnostics)

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
            # Local Modbus-only diagnostic metadata (skipped blocks, last error, family).
            "modbus_diagnostics": modbus_diag,
        }

    return async_redact_data(
        {
            "entry_id": entry.entry_id,
            "gateway": entry.data.get("gateway"),
            "app_id": entry.data.get("app_id"),
            "tokens_present": "tokens" in entry.data,
            "options": dict(entry.options),
            "plants": plant_data,
        },
        TO_REDACT,
    )
