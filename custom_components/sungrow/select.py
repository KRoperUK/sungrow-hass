"""Select entities for Sungrow iSolarCloud dispatch control."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from . import async_start_heartbeat, async_stop_heartbeat, select_dispatch_device
from .const import DOMAIN
from .coordinator import SungrowPlantCoordinator

_LOGGER = logging.getLogger(__name__)

# Dispatch writes go to a single device via one Control client; serialise them.
PARALLEL_UPDATES = 1

# Select parameters exposed as HA Select entities.
DISPATCH_SELECTS: dict[str, dict[str, Any]] = {
    "charge_discharge_command": {
        "options_map": {
            "Stop": Control.CHARGE_DISCHARGE_COMMANDS["stop"],
            "Charge": Control.CHARGE_DISCHARGE_COMMANDS["charge"],
            "Discharge": Control.CHARGE_DISCHARGE_COMMANDS["discharge"],
        },
    },
    "forced_charging": {
        "options_map": {
            "Disable": Control.FORCED_CHARGING["disable"],
            "Enable": Control.FORCED_CHARGING["enable"],
        },
        # Enabling/disabling forced charging is a policy setting, not actuation.
        "entity_category": EntityCategory.CONFIG,
    },
}


def _build_selects(coordinator: SungrowPlantCoordinator, control: Control) -> list[SelectEntity]:
    """Build the dispatch select entities for a coordinator's target device.

    Returns an empty list when no dispatch-capable device is present. Reads the
    coordinator's live device list so a dispatchable device that appears after
    setup gets its controls at runtime (dynamic-devices).
    """
    # Prefer the ESS device if present, otherwise fall back to an inverter.
    target = select_dispatch_device(coordinator.devices)
    if target is None:
        return []
    device_uuid = target.get("uuid")
    if not device_uuid:
        return []
    device_name = target.get("device_name") or coordinator.plant_name
    return [
        SungrowDispatchSelect(coordinator, control, device_uuid, device_name, param, meta)
        for param, meta in DISPATCH_SELECTS.items()
    ]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow dispatch select entities."""
    data = entry.runtime_data
    control = data.control
    coordinators = data.coordinators

    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[SelectEntity] = []
        for coordinator in coordinators:
            for entity in _build_selects(coordinator, control):
                uid = entity.unique_id
                if uid is None or uid in known_unique_ids:
                    continue
                known_unique_ids.add(uid)
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    _add_new_entities()
    for coordinator in coordinators:
        entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class SungrowDispatchSelect(CoordinatorEntity[SungrowPlantCoordinator], RestoreEntity, SelectEntity):
    """Select entity for a Sungrow dispatch parameter."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        control: Control,
        device_uuid: str,
        device_name: str,
        param: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialize the dispatch select."""
        super().__init__(coordinator)
        self.control = control
        self.device_uuid = device_uuid
        self.param = param
        self.options_map = dict(meta["options_map"])
        reverse_map = {v: k for k, v in self.options_map.items()}
        self._reverse_map = reverse_map
        # Entity name comes from translations (entity.select.<param>.name).
        self._attr_translation_key = param
        self._attr_unique_id = f"{coordinator.plant_id}_{device_uuid}_{param}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            name=device_name,
            manufacturer="Sungrow",
            # Nest the dispatch device under the plant device the sensors created.
            via_device=(DOMAIN, coordinator.plant_id),
        )
        self._attr_options = list(self.options_map.keys())
        self._attr_entity_category = meta.get("entity_category")

    async def async_added_to_hass(self) -> None:
        """Restore the last selected option across restarts.

        Dispatch commands are write-only (not polled back from the API), so the
        last option the user chose is restored from state rather than fetched.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.options_map:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        """Update the dispatch parameter on the inverter."""
        value = self.options_map[option]
        _LOGGER.debug("Setting %s to %s (%s) for %s", self.param, option, value, self.device_uuid)
        try:
            await self.control.async_update_parameters(self.device_uuid, {self.param: value})
        except PySolarCloudException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dispatch_write_failed",
                translation_placeholders={"param": self.param, "error": str(err)},
            ) from err
        # Remember the selected option so the UI reflects it and it survives restarts.
        self._attr_current_option = option

        # Keep the inverter in dispatch mode while actively charging/discharging.
        entry = self.coordinator.config_entry
        assert entry is not None
        if self.param == "charge_discharge_command" and option in {"Charge", "Discharge"}:
            await async_start_heartbeat(
                self.hass,
                entry,
                self.coordinator.plant_id,
                self.device_uuid,
                interval=60,
            )
        elif self.param == "charge_discharge_command" and option == "Stop":
            await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)

    async def async_will_remove_from_hass(self) -> None:
        """Stop the heartbeat when the entity is removed."""
        entry = self.coordinator.config_entry
        assert entry is not None
        await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
        await super().async_will_remove_from_hass()
