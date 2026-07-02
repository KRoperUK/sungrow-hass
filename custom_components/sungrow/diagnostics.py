"""Diagnostics support for the Sungrow iSolarCloud integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SungrowConfigEntry, SungrowData


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SungrowConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Tokens and secrets are redacted; raw coordinator data and device lists are
    included to help identify unsupported point IDs (e.g. EV chargers).
    """
    data = getattr(entry, "runtime_data", None)
    coordinators = data.coordinators if isinstance(data, SungrowData) else []
    devices_by_plant = data.devices if isinstance(data, SungrowData) else {}

    plant_data: dict[str, dict[str, Any]] = {}
    for coordinator in coordinators:
        plant_data[coordinator.plant_id] = {
            "plant_name": coordinator.plant_name,
            "last_update_success": coordinator.last_update_success,
            "data": coordinator.data,
            "devices": devices_by_plant.get(coordinator.plant_id, []),
        }

    return {
        "entry_id": entry.entry_id,
        "gateway": entry.data.get("gateway"),
        "app_id": entry.data.get("app_id"),
        "tokens_present": "tokens" in entry.data,
        "options": dict(entry.options),
        "plants": plant_data,
    }
