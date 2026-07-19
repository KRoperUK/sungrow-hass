"""Diagnostics support for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SungrowConfigEntry, SungrowData
from ._serialization import anonymise_device_keys, catalog_rows, jsonable
from .model_capabilities import resolve_capabilities

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


def build_points_catalog(
    plant_realtime: Any,
    device_realtime: dict[str, Any],
    models_by_type: dict[str, str | None],
) -> dict[str, Any]:
    """Build a human-readable catalog of every point the API reports (#252).

    Turns the raw plant + per-device realtime dumps into flat, sorted point lists so a
    user can see exactly which point IDs are available on their hardware and copy them
    into the "Extra measure points" option, instead of hunting in the developer portal.
    Each device type is annotated with the resolved model family / battery capability
    (#251) so it is obvious which device a point belongs to. Contains only point
    metadata and model codes — no uuids, serials or user-set names — so it is safe to
    include in a shared diagnostics bundle.
    """
    catalog: dict[str, Any] = {"plant": catalog_rows(plant_realtime), "devices": {}}
    for type_id, per_device in device_realtime.items():
        # Merge points across all devices of this type, deduped by point id (devices of
        # one type report the same point set). ``per_device`` is ``{device_N: {code:
        # point}}`` on success, or ``{"error": ...}`` when the probe failed.
        merged: dict[str, dict[str, Any]] = {}
        if isinstance(per_device, dict):
            for payload in per_device.values():
                for row in catalog_rows(payload):
                    merged.setdefault(row["point_id"], row)
        model = models_by_type.get(type_id)
        caps = resolve_capabilities(model)
        catalog["devices"][type_id] = {
            "model": model,
            "family": caps.family.value,
            "has_battery": caps.has_battery,
            "points": sorted(merged.values(), key=lambda row: row["point_id"]),
        }
    return catalog


async def _probe_plant_devices(
    service: Any, plant_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str | None]]:
    """Best-effort capture of every device and its per-device realtime data.

    Returns ``(all_devices, device_realtime, models_by_type)``. The plant realtime
    endpoint only returns point IDs the library knows about, so hardware like EV
    chargers never shows up as sensors (issue #18). This walks the *full* device list
    (not just inverter/ESS) and attempts a per-device realtime fetch for each distinct
    type, so a diagnostics download reveals an unmapped device's type and reachable
    points — enough to add a proper mapping. ``models_by_type`` maps each device-type id
    to that type's model code, so the point catalog can annotate device families (#252).
    All failures are captured, never raised, so a diagnostics download always succeeds.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            all_devices = await service.async_get_plant_devices(plant_id)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Diagnostics: could not list devices for plant %s: %s", plant_id, err)
        return [{"error": str(err)}], {}, {}

    device_realtime: dict[str, Any] = {}
    models_by_type: dict[str, str | None] = {}
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
        # Record the model code for this type (first device of the type wins), for the
        # point-catalog family annotation (#252).
        models_by_type.setdefault(str(type_id), device.get("device_model_code"))
        if type_id in seen_types:
            continue
        seen_types.add(type_id)
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                realtime = await service.async_get_device_realtime(plant_id, device_type)
            device_realtime[str(type_id)] = anonymise_device_keys(jsonable(realtime), uuid_map)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Diagnostics: device realtime failed for type %s: %s", type_id, err)
            device_realtime[str(type_id)] = {"error": str(err)}

    return jsonable(all_devices), device_realtime, models_by_type


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SungrowConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Tokens and secrets are redacted; raw coordinator data, the full device list,
    and per-device realtime data are included to help identify unsupported point
    IDs (e.g. EV chargers — see issue #18). A flattened ``points_catalog`` lists
    every reported point (id/name/value/unit) per device so users can pick point
    IDs for the "Extra measure points" option without portal-spelunking (#252).
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
        models_by_type: dict[str, str | None] = {}
        if service is not None:
            all_devices, device_realtime, models_by_type = await _probe_plant_devices(service, plant_id)

        modbus_diag: dict[str, Any] = {}
        if getattr(coordinator, "modbus_diagnostics", None):
            modbus_diag = dict(coordinator.modbus_diagnostics)

        plant_data[plant_id] = {
            "plant_name": coordinator.plant_name,
            "last_update_success": coordinator.last_update_success,
            "data": coordinator.data,
            # Devices the integration created dispatch entities for (inverter/ESS).
            "devices": jsonable(devices_by_plant.get(plant_id, [])),
            # Every device on the plant, including hardware without mapped sensors.
            "all_devices": all_devices,
            # Per-device-type realtime data (best effort; {} where unsupported).
            "device_realtime": device_realtime,
            # Flattened, human-readable catalog of every reported point (#252): the
            # point IDs a user can copy into the "Extra measure points" option.
            "points_catalog": build_points_catalog(coordinator.data, device_realtime, models_by_type),
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
