"""Select entities for Sungrow iSolarCloud dispatch control."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud.control import Control

from . import async_start_heartbeat, async_stop_heartbeat
from .const import DOMAIN
from .coordinator import SungrowPlantCoordinator

_LOGGER = logging.getLogger(__name__)

# Select parameters exposed as HA Select entities.
DISPATCH_SELECTS: dict[str, dict] = {
    "charge_discharge_command": {
        "name": "Charge/Discharge Command",
        "options_map": {
            "Stop": Control.CHARGE_DISCHARGE_COMMANDS["stop"],
            "Charge": Control.CHARGE_DISCHARGE_COMMANDS["charge"],
            "Discharge": Control.CHARGE_DISCHARGE_COMMANDS["discharge"],
        },
    },
    "forced_charging": {
        "name": "Forced Charging",
        "options_map": {
            "Disable": Control.FORCED_CHARGING["disable"],
            "Enable": Control.FORCED_CHARGING["enable"],
        },
    },
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow dispatch select entities."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    control: Control = entry_data["control"]
    devices_by_plant: dict[str, list[dict]] = entry_data["devices"]
    coordinators: list[SungrowPlantCoordinator] = entry_data["coordinators"]

    entities: list[SelectEntity] = []
    for coordinator in coordinators:
        plant_id = coordinator.plant_id
        devices = devices_by_plant.get(plant_id, [])
        if not devices:
            continue
        ess_devices = [d for d in devices if d.get("device_type") == "ENERGY_STORAGE_SYSTEM"]
        target = ess_devices[0] if ess_devices else devices[0]
        device_uuid = target.get("uuid")
        if not device_uuid:
            continue
        device_name = target.get("device_name") or coordinator.plant_name
        for param, meta in DISPATCH_SELECTS.items():
            entities.append(
                SungrowDispatchSelect(
                    coordinator,
                    control,
                    device_uuid,
                    device_name,
                    param,
                    meta,
                )
            )

    async_add_entities(entities)


class SungrowDispatchSelect(CoordinatorEntity, SelectEntity):
    """Select entity for a Sungrow dispatch parameter."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        control: Control,
        device_uuid: str,
        device_name: str,
        param: str,
        meta: dict,
    ) -> None:
        """Initialize the dispatch select."""
        super().__init__(coordinator)
        self.control = control
        self.device_uuid = device_uuid
        self.param = param
        self.options_map = dict(meta["options_map"])
        reverse_map = {v: k for k, v in self.options_map.items()}
        self._reverse_map = reverse_map
        self._attr_name = meta["name"]
        self._attr_unique_id = f"{coordinator.plant_id}_{device_uuid}_{param}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            name=device_name,
            manufacturer="Sungrow",
        )
        self._attr_options = list(self.options_map.keys())

    @property
    def current_option(self) -> str | None:
        """Return the current option.

        Dispatch commands are write-only (not polled back from the API), so the
        current option is unknown. Availability is inherited from
        CoordinatorEntity (tracks ``coordinator.last_update_success``).
        """
        return None

    async def async_select_option(self, option: str) -> None:
        """Update the dispatch parameter on the inverter."""
        value = self.options_map[option]
        _LOGGER.debug("Setting %s to %s (%s) for %s", self.param, option, value, self.device_uuid)
        await self.control.async_update_parameters(self.device_uuid, {self.param: value})

        # Keep the inverter in dispatch mode while actively charging/discharging.
        if self.param == "charge_discharge_command" and option in {"Charge", "Discharge"}:
            await async_start_heartbeat(
                self.hass,
                self.coordinator.config_entry,
                self.coordinator.plant_id,
                self.device_uuid,
                interval=60,
            )
        elif self.param == "charge_discharge_command" and option == "Stop":
            await async_stop_heartbeat(self.hass, self.coordinator.config_entry, self.coordinator.plant_id)

    async def async_will_remove_from_hass(self) -> None:
        """Stop the heartbeat when the entity is removed."""
        await async_stop_heartbeat(self.hass, self.coordinator.config_entry, self.coordinator.plant_id)
        await super().async_will_remove_from_hass()
