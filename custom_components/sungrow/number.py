"""Number entities for Sungrow iSolarCloud dispatch control."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
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

# Dispatch writes go to a single device via one Control client; serialise them so
# rapid slider changes don't race on the API.
PARALLEL_UPDATES = 1

# Conservative default upper bound (watts) for charge/discharge power. iSolarCloud
# does not expose the per-device inverter rating through the realtime API, so this
# is a fixed clamp rather than a derived limit. Users with larger inverters can
# still command higher power via the underlying API; adjust here if a rating source
# becomes available.
DEFAULT_MAX_DISPATCH_POWER = 5000

# Number parameters exposed as HA Number entities.
# Keys are canonical Control parameter names; values describe the HA entity.
DISPATCH_NUMBERS: dict[str, dict] = {
    "charge_discharge_power": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
    },
    "soc_upper_limit": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 70,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
    },
    "soc_lower_limit": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 50,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
    },
    "forced_charging_target_soc_1": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
    },
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow dispatch number entities."""
    data = entry.runtime_data
    control = data.control
    devices_by_plant = data.devices
    coordinators = data.coordinators

    entities: list[NumberEntity] = []
    for coordinator in coordinators:
        plant_id = coordinator.plant_id
        devices = devices_by_plant.get(plant_id, [])
        if not devices:
            continue
        # Prefer the ESS device if present, otherwise fall back to the first inverter.
        ess_devices = [d for d in devices if d.get("device_type") == "ENERGY_STORAGE_SYSTEM"]
        target = ess_devices[0] if ess_devices else devices[0]
        device_uuid = target.get("uuid")
        if not device_uuid:
            continue
        device_name = target.get("device_name") or coordinator.plant_name
        for param, meta in DISPATCH_NUMBERS.items():
            entities.append(
                SungrowDispatchNumber(
                    coordinator,
                    control,
                    device_uuid,
                    device_name,
                    param,
                    meta,
                )
            )

    async_add_entities(entities)


class SungrowDispatchNumber(CoordinatorEntity, NumberEntity):
    """Number entity for a Sungrow dispatch parameter."""

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
        """Initialize the dispatch number."""
        super().__init__(coordinator)
        self.control = control
        self.device_uuid = device_uuid
        self.param = param
        # Entity name comes from translations (entity.number.<param>.name).
        self._attr_translation_key = param
        self._attr_unique_id = f"{coordinator.plant_id}_{device_uuid}_{param}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            name=device_name,
            manufacturer="Sungrow",
            # Nest the dispatch device under the plant device the sensors created.
            via_device=(DOMAIN, coordinator.plant_id),
        )
        self._attr_device_class = meta.get("device_class")
        self._attr_native_unit_of_measurement = meta.get("native_unit_of_measurement")
        self._attr_native_min_value = meta["native_min_value"]
        self._attr_native_max_value = meta["native_max_value"]
        self._attr_native_step = meta["native_step"]
        self._attr_mode = meta["mode"]

    @property
    def native_value(self) -> float | None:
        """Return the current parameter value.

        Dispatch parameters are write-only here (not polled back from the API),
        so there is no meaningful value to report. Availability is inherited from
        CoordinatorEntity (tracks ``coordinator.last_update_success``).
        """
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the dispatch parameter on the inverter."""
        _LOGGER.debug("Setting %s to %s for %s", self.param, value, self.device_uuid)
        await self.control.async_update_parameters(self.device_uuid, {self.param: str(int(value))})
        # If the user is actively dispatching, ensure a heartbeat is running so the
        # inverter stays in External EMS mode.
        if self.param == "charge_discharge_power":
            await async_start_heartbeat(
                self.hass,
                self.coordinator.config_entry,
                self.coordinator.plant_id,
                self.device_uuid,
                interval=60,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the heartbeat when the entity is removed."""
        await async_stop_heartbeat(self.hass, self.coordinator.config_entry, self.coordinator.plant_id)
        await super().async_will_remove_from_hass()
