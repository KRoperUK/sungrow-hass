"""Number entities for Sungrow iSolarCloud dispatch control."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from . import async_start_heartbeat, async_stop_heartbeat, select_dispatch_device
from .const import DOMAIN
from .coordinator import SungrowPlantCoordinator

_LOGGER = logging.getLogger(__name__)

# Dispatch writes go to a single device via one Control client; serialise them so
# rapid slider changes don't race on the API.
PARALLEL_UPDATES = 1

# Fallback upper bound (watts) for charge/discharge power, used when the device's
# rated power can't be derived from its model code.
DEFAULT_MAX_DISPATCH_POWER = 5000

# Sungrow residential inverters encode their kW rating in the model code, e.g.
# SG3.6RS -> 3.6 kW, SH10RT-V112 -> 10 kW, SG110CX -> 110 kW. Batteries, meters and
# comms modules (SBR256, SGSmartMeter, WiNet-S) don't match and fall back to the
# default. This is the only rating signal iSolarCloud exposes via getDeviceListByPsId.
_MODEL_POWER_RE = re.compile(r"S[GH](\d+(?:\.\d+)?)", re.IGNORECASE)


def rated_power_w(device: dict[str, Any]) -> int | None:
    """Best-effort rated power in watts parsed from a device's model code.

    Returns ``None`` when no rating can be parsed, so callers fall back to the
    default clamp.
    """
    match = _MODEL_POWER_RE.match(str(device.get("device_model_code") or ""))
    if not match:
        return None
    try:
        kw = float(match.group(1))
    except ValueError:
        return None
    if kw <= 0 or kw > 1000:  # guard against nonsense parses
        return None
    return int(round(kw * 1000))


# Number parameters exposed as HA Number entities.
# Keys are canonical Control parameter names; values describe the HA entity.
DISPATCH_NUMBERS: dict[str, dict[str, Any]] = {
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
        # SOC limits set battery policy rather than actuate — configuration entities.
        "entity_category": EntityCategory.CONFIG,
    },
    "soc_lower_limit": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 50,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    "forced_charging_target_soc_1": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Battery power caps: the maximum power the ESS will charge/discharge at. Watts,
    # same value format as charge_discharge_power, sized to the device's rating.
    "max_charging_power": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    "max_discharging_power": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Target SOC for the second forced-charging window (mirrors ..._soc_1).
    "forced_charging_target_soc_2": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Export (feed-in) limit as an absolute power. Only takes effect when the
    # feed_in_limitation select is enabled. Watts UI, sent as kW, sized to rating.
    "feed_in_limitation_value": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Export limit as a percentage of rated power (0-100%).
    "feed_in_limitation_ratio": {
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Active power output cap as a percentage of rated power (0-100%).
    "active_power_limit_ratio": {
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
}

# Power parameters: sent to the API in kW (converted from the entities' W), and
# their slider maximum is sized to the device's rated power.
_RATED_POWER_PARAMS = frozenset(
    {"charge_discharge_power", "max_charging_power", "max_discharging_power", "feed_in_limitation_value"}
)


def _build_numbers(coordinator: SungrowPlantCoordinator, control: Control) -> list[NumberEntity]:
    """Build the dispatch number entities for a coordinator's target device.

    Returns an empty list when no dispatch-capable device is present. Reads the
    coordinator's live device list so a dispatchable device that appears after
    setup gets its controls at runtime (dynamic-devices).
    """
    # Skip entirely if the device reported that it doesn't accept parameter writes.
    if not coordinator.dispatch_update_supported:
        return []
    # Prefer the ESS device if present, otherwise fall back to an inverter.
    target = select_dispatch_device(coordinator.devices)
    if target is None:
        return []
    device_uuid = target.get("uuid")
    if not device_uuid:
        return []
    device_name = target.get("device_name") or coordinator.plant_name
    # Size the power sliders to the device's rated power when it can be derived
    # from the model code; otherwise use the conservative default.
    max_power = rated_power_w(target) or DEFAULT_MAX_DISPATCH_POWER
    entities: list[NumberEntity] = []
    for param, meta in DISPATCH_NUMBERS.items():
        if param in _RATED_POWER_PARAMS and max_power != meta["native_max_value"]:
            meta = {**meta, "native_max_value": max_power}
        entities.append(SungrowDispatchNumber(coordinator, control, device_uuid, device_name, param, meta))
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow dispatch number entities."""
    data = entry.runtime_data
    control = data.control
    coordinators = data.coordinators

    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[NumberEntity] = []
        for coordinator in coordinators:
            for entity in _build_numbers(coordinator, control):
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


class SungrowDispatchNumber(CoordinatorEntity[SungrowPlantCoordinator], RestoreNumber):
    """Number entity for a Sungrow dispatch parameter."""

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
        self._attr_entity_category = meta.get("entity_category")

    async def async_added_to_hass(self) -> None:
        """Restore the last commanded value across restarts.

        Dispatch parameters are write-only (not polled back from the API), so the
        last value the user set is restored from state rather than fetched.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Update the dispatch parameter on the inverter."""
        _LOGGER.debug("Setting %s to %s for %s", self.param, value, self.device_uuid)
        # The API expects power parameters in kW (the entities present them in W for a
        # familiar UI), and the client sends the value verbatim — so convert W->kW on
        # the way out. Percentage/other params are sent as integers.
        wire_value = str(round(value / 1000, 2)) if self.param in _RATED_POWER_PARAMS else str(int(value))
        try:
            await self.control.async_update_parameters(self.device_uuid, {self.param: wire_value})
        except PySolarCloudException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dispatch_write_failed",
                translation_placeholders={"param": self.param, "error": str(err)},
            ) from err
        # Remember the commanded value so the UI reflects it and it survives restarts.
        self._attr_native_value = value
        # If the user is actively dispatching, ensure a heartbeat is running so the
        # inverter stays in External EMS mode.
        if self.param == "charge_discharge_power":
            entry = self.coordinator.config_entry
            assert entry is not None
            await async_start_heartbeat(
                self.hass,
                entry,
                self.coordinator.plant_id,
                self.device_uuid,
                interval=60,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Stop the heartbeat when the entity is removed."""
        entry = self.coordinator.config_entry
        assert entry is not None
        await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
        await super().async_will_remove_from_hass()
