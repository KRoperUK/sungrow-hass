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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from . import DispatchControl, build_device_info, select_dispatch_device
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


# Number parameters exposed as HA Number entities. Keys are canonical Control
# parameter names. The entities present values in their natural unit (watts,
# percent); the raw value the API expects is produced by pysolarcloud's
# Control.encode_parameter (which knows, from the docs' Appendix 10, that power is
# watts, SOC/ratios are tenths of a percent, etc.) — so the encoding lives in one
# place instead of being duplicated here.
DISPATCH_NUMBERS: dict[str, dict[str, Any]] = {
    "charge_discharge_power": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        # Battery actuation: meaningless (and harmful — see #148) without a battery.
        "battery_only": True,
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
        "battery_only": True,
    },
    "soc_lower_limit": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 50,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    "forced_charging_target_soc_1": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
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
        "battery_only": True,
    },
    # Export (feed-in) limit as an absolute power in watts. Only takes effect when
    # the feed_in_limitation select is enabled. Sized to the device's rating.
    "feed_in_limitation_value": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Export limit as a percentage of rated power (API range 0-1000 = 0-100%).
    "feed_in_limitation_ratio": {
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Active power output cap as a percentage of rated power (API range 0-1000).
    "active_power_limit_ratio": {
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Reactive power ratio Q(t) as a signed percentage (API range -600..600 = -60..60%).
    # Only takes effect when the Reactive Power Mode select is set to Q(t). Applies to
    # PV and hybrid inverters, so not battery-gated.
    "q_t": {
        "native_unit_of_measurement": "%",
        "native_min_value": -60,
        "native_max_value": 60,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Power factor setpoint (API range -1000..1000 = -1..1). Only takes effect when the
    # Reactive Power Mode select is set to PF.
    "pf": {
        "device_class": NumberDeviceClass.POWER_FACTOR,
        "native_min_value": -1,
        "native_max_value": 1,
        "native_step": 0.01,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
}

# Watt-valued power parameters whose slider maximum is sized to the device's rating.
_RATED_POWER_PARAMS = frozenset({"charge_discharge_power", "feed_in_limitation_value"})


def _build_numbers(coordinator: SungrowPlantCoordinator, control: DispatchControl | None) -> list[NumberEntity]:
    """Build the dispatch number entities for a coordinator's target device.

    Returns an empty list when no dispatch-capable device is present. Reads the
    coordinator's live device list so a dispatchable device that appears after
    setup gets its controls at runtime (dynamic-devices).
    """
    if control is None:
        return []
    # Skip entirely if the device reported that it doesn't accept parameter writes.
    if not coordinator.dispatch_update_supported:
        return []
    # Prefer the ESS device if present, otherwise fall back to an inverter.
    target = select_dispatch_device(coordinator.devices)
    if target is None:
        return []
    if not target.get("uuid"):
        return []
    # Size the power sliders to the device's rated power when it can be derived
    # from the model code; otherwise use the conservative default.
    max_power = rated_power_w(target) or DEFAULT_MAX_DISPATCH_POWER
    entities: list[NumberEntity] = []
    for param, meta in DISPATCH_NUMBERS.items():
        # Hide battery-only controls on PV-only plants — see #148.
        if meta.get("battery_only") and not coordinator.has_battery:
            continue
        if param in _RATED_POWER_PARAMS and max_power != meta["native_max_value"]:
            meta = {**meta, "native_max_value": max_power}
        entities.append(SungrowDispatchNumber(coordinator, control, target, param, meta))
    # The forced-dispatch auto-revert timeout only makes sense alongside the battery
    # charge/discharge controls, so gate it on the same has_battery check (#157/#148).
    if coordinator.has_battery:
        entities.append(SungrowForcedDispatchDurationNumber(coordinator, target))
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
    # Dispatch parameters are write-only: the API doesn't read the current setpoint back
    # (getDevPropertyPointValue is permission-gated), so the value shown is the last one
    # we commanded — an assumption, not a device reading. Unset until first set/restored,
    # which correctly reads as "unknown" per HA's entity-unavailable guidance.
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        control: DispatchControl,
        device: dict[str, Any],
        param: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialize the dispatch number."""
        super().__init__(coordinator)
        self.control = control
        self.device_uuid = str(device["uuid"])
        self.param = param
        # Entity name comes from translations (entity.number.<param>.name).
        self._attr_translation_key = param
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_{param}"
        # Nest the dispatch device under the plant device the sensors created,
        # enriched with the model/serial the cloud reports.
        self._attr_device_info = build_device_info(device, coordinator.plant_id, fallback_name=coordinator.plant_name)
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
        # Encode the displayed value into the raw value the API expects (watts,
        # tenths-of-a-percent, etc.) using pysolarcloud's authoritative specs.
        wire_value = Control.encode_parameter(self.param, value)
        try:
            await self.control.async_update_parameters(self.device_uuid, {self.param: wire_value})
        except PySolarCloudException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dispatch_write_failed",
                translation_placeholders={"param": self.param, "error": str(err)},
            ) from err
        # Remember the commanded value so the UI reflects it and it survives restarts.
        # Writing power only sets the target: the EMS heartbeat is owned solely by the
        # command select (Charge/Discharge start it, Stop stops it), so writing power
        # — even 0 — never arms or re-arms dispatch here (see #112).
        self._attr_native_value = value


# Default duration (minutes) for a forced Charge/Discharge before auto-revert (#157 / #255).
# A non-zero default means forced commands always have a bounded lifetime out of the box;
# users can still set 0 to opt out of auto-revert.
DEFAULT_FORCED_DISPATCH_DURATION = 60


class SungrowForcedDispatchDurationNumber(CoordinatorEntity[SungrowPlantCoordinator], RestoreNumber):
    """Local number controlling the forced-dispatch auto-revert timeout (#157 / #255).

    Unlike the other dispatch numbers this writes *nothing* to the inverter — it only
    records, on the coordinator, how long a forced Charge/Discharge command may stay
    active before the command select reverts it to Stop. 0 disables auto-revert.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "forced_dispatch_duration"
    _attr_native_min_value = 0
    _attr_native_max_value = 1440  # 24 h
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SungrowPlantCoordinator, device: dict[str, Any]) -> None:
        """Initialize the forced-dispatch duration number."""
        super().__init__(coordinator)
        self.device_uuid = str(device["uuid"])
        # Identifies the entity (like the dispatch numbers) though it writes no param.
        self.param = "forced_dispatch_duration"
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_forced_dispatch_duration"
        self._attr_device_info = build_device_info(device, coordinator.plant_id, fallback_name=coordinator.plant_name)
        self._attr_native_value = DEFAULT_FORCED_DISPATCH_DURATION
        coordinator.forced_dispatch_duration_minutes = DEFAULT_FORCED_DISPATCH_DURATION

    async def async_added_to_hass(self) -> None:
        """Restore the configured duration and publish it to the coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self.coordinator.forced_dispatch_duration_minutes = self._attr_native_value or 0

    async def async_set_native_value(self, value: float) -> None:
        """Store the new duration locally and publish it to the coordinator."""
        self._attr_native_value = value
        self.coordinator.forced_dispatch_duration_minutes = value
