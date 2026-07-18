"""Binary sensors for the Sungrow iSolarCloud integration.

A per-device ``problem`` binary sensor derived from each device's fault status
(``dev_fault_status`` from the device list): the "is something wrong?" signal that
was missing when a plant silently produced nothing (#151).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud.plants import DeviceType

from . import build_device_info
from .coordinator import SungrowPlantCoordinator
from .measure_points import resolve_enum_value
from .modbus_registers import POWER_FLOW_STATUS_BITS

# Read-only, updated in bulk by the coordinator.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# Fault-status forms that mean "problem" / "no problem". pysolarcloud converts the
# raw code to a ``DeviceFaultStaus`` enum (FAULT=1, ALARM=2, NORMAL=4), but the raw
# API and test mocks may present an int or a string, so match every representation.
_OK_STATES = {"4", "NORMAL"}
_PROBLEM_STATES = {"1", "2", "FAULT", "ALARM"}


def fault_is_on(status: Any) -> bool | None:
    """Map a device's ``dev_fault_status`` to a PROBLEM on/off, or None if unknown.

    NORMAL -> off, FAULT/ALARM -> on, anything unrecognised -> None (unknown) rather
    than a misleading "no problem".
    """
    if status is None:
        return None
    text = str(getattr(status, "name", status))
    if text in _OK_STATES:
        return False
    if text in _PROBLEM_STATES:
        return True
    return None


def connectivity_is_on(status: Any) -> bool | None:
    """Map a device's ``dev_status`` to online (True) / offline (False), or None if unknown.

    The device list reports ``dev_status`` as "1" (online) / "0" (offline); the int forms
    are tolerated too.
    """
    if status is None:
        return None
    text = str(status)
    if text == "1":
        return True
    if text == "0":
        return False
    return None


def _build_binary_sensors(coordinator: SungrowPlantCoordinator) -> list[BinarySensorEntity]:
    """Build the fault + connectivity binary sensors for every device the plant reports."""
    sensors: list[BinarySensorEntity] = []
    if coordinator.plants_service is None:
        # Modbus-only path: the local inverter's connectivity is driven by the last poll.
        data = coordinator.data or {}
        has_power_flow = "power_flow_status" in data
        for device in coordinator.devices:
            if device.get("device_type") == DeviceType.INVERTER and device.get("uuid"):
                sensors.append(SungrowModbusConnectivityBinarySensor(coordinator, device))
                # Hybrid families expose power_flow_status; string inverters don't,
                # so we only create the per-flow binary sensors when the register
                # is actually present in the coordinator's data (#326).
                if has_power_flow:
                    sensors.extend(
                        SungrowModbusPowerFlowBinarySensor(coordinator, device, bit, key)
                        for bit, key in POWER_FLOW_STATUS_BITS
                    )
        return sensors

    for device in coordinator.devices:
        if not device.get("uuid"):
            continue
        sensors.append(SungrowDeviceFaultBinarySensor(coordinator, device))
        sensors.append(SungrowDeviceConnectivityBinarySensor(coordinator, device))
    return sensors


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow binary sensors from the coordinators created during entry setup."""
    coordinators = entry.runtime_data.coordinators

    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[BinarySensorEntity] = []
        for coordinator in coordinators:
            for entity in _build_binary_sensors(coordinator):
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


class SungrowDeviceFaultBinarySensor(CoordinatorEntity[SungrowPlantCoordinator], BinarySensorEntity):
    """A ``problem`` binary sensor reflecting a device's fault/alarm status."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_fault"

    def __init__(self, coordinator: SungrowPlantCoordinator, device: dict[str, Any]) -> None:
        """Initialize the fault binary sensor for a device."""
        super().__init__(coordinator)
        self.device_uuid = str(device["uuid"])
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_fault"
        self._attr_device_info = build_device_info(device, coordinator.plant_id, fallback_name=coordinator.plant_name)

    def _device(self) -> dict[str, Any] | None:
        """Return this sensor's device from the coordinator's live list, if still present."""
        return next((d for d in self.coordinator.devices if str(d.get("uuid")) == self.device_uuid), None)

    @property
    def available(self) -> bool:
        """Unavailable if the poll failed or the device dropped out of the plant."""
        return super().available and self._device() is not None

    @property
    def is_on(self) -> bool | None:
        """True when the device reports a fault or alarm."""
        device = self._device()
        if device is None:
            return None
        return fault_is_on(device.get("dev_fault_status"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw fault status plus a human-readable operating-status reason."""
        device = self._device() or {}
        status = device.get("dev_fault_status")
        return {
            "fault_status": str(getattr(status, "name", status)) if status is not None else None,
            "operating_status": self._operating_status_reason(),
        }

    def _operating_status_reason(self) -> str | None:
        """Human-readable operating status for this device, if reported (#182).

        Sourced from the operating-status measure point (inverter point 29 / ESS 13146)
        the coordinator always fetches, so it explains *why* a device is in a problem
        state ("Shut down due to faults", "Low insulation resistance", ...). ``None`` for
        devices that report no operating status (battery/meter/comm) or when the
        per-device endpoint is unavailable.
        """
        device_data = getattr(self.coordinator, "device_data", None) or {}
        point = device_data.get(self.device_uuid, {}).get("operating_status")
        if not isinstance(point, dict):
            return None
        return resolve_enum_value(str(point.get("id") or ""), point.get("value"))


class SungrowDeviceConnectivityBinarySensor(CoordinatorEntity[SungrowPlantCoordinator], BinarySensorEntity):
    """A ``connectivity`` binary sensor reflecting whether a device is online."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_connectivity"

    def __init__(self, coordinator: SungrowPlantCoordinator, device: dict[str, Any]) -> None:
        """Initialize the connectivity binary sensor for a device."""
        super().__init__(coordinator)
        self.device_uuid = str(device["uuid"])
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_online"
        self._attr_device_info = build_device_info(device, coordinator.plant_id, fallback_name=coordinator.plant_name)

    def _device(self) -> dict[str, Any] | None:
        """Return this sensor's device from the coordinator's live list, if still present."""
        return next((d for d in self.coordinator.devices if str(d.get("uuid")) == self.device_uuid), None)

    @property
    def available(self) -> bool:
        """Unavailable only if the poll failed or the device dropped out of the plant."""
        return super().available and self._device() is not None

    @property
    def is_on(self) -> bool | None:
        """True when the device reports itself online (``dev_status``)."""
        device = self._device()
        if device is None:
            return None
        return connectivity_is_on(device.get("dev_status"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose static device details (commissioning date) for the device page."""
        device = self._device() or {}
        return {"commissioning_date": device.get("grid_connection_date")}


class SungrowModbusConnectivityBinarySensor(CoordinatorEntity[SungrowPlantCoordinator], BinarySensorEntity):
    """Connectivity sensor for a local Modbus inverter driven by poll success."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SungrowPlantCoordinator, device: dict[str, Any]) -> None:
        """Initialize the Modbus connectivity sensor for a local inverter."""
        super().__init__(coordinator)
        self.device_uuid = str(device["uuid"])
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_online"
        local_url = getattr(coordinator, "local_configuration_url", None)
        self._attr_device_info = build_device_info(
            device,
            coordinator.plant_id,
            fallback_name=coordinator.plant_name,
            via_plant_id=getattr(coordinator, "via_plant_id", None),
            configuration_url=local_url if isinstance(local_url, str) else None,
        )

    @property
    def is_on(self) -> bool | None:
        """True when the last Modbus poll succeeded."""
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose Modbus diagnostics (skipped blocks, last error) for troubleshooting."""
        diag = getattr(self.coordinator, "modbus_diagnostics", {}) or {}
        attrs: dict[str, Any] = {}
        if diag.get("device_family"):
            attrs["device_family"] = diag["device_family"]
        if diag.get("skipped_blocks"):
            attrs["skipped_blocks"] = diag["skipped_blocks"]
        if diag.get("last_error"):
            attrs["last_error"] = diag["last_error"]
        return attrs


# Bit-index -> HA device class for the power_flow_status flags. Only two of the
# five surfaced bits have a natural HA device class; the rest present as plain
# translated boolean sensors and get their name from strings.json.
_POWER_FLOW_DEVICE_CLASSES: dict[int, BinarySensorDeviceClass] = {
    0: BinarySensorDeviceClass.RUNNING,  # PV generating
    1: BinarySensorDeviceClass.BATTERY_CHARGING,  # Battery charging
}


class SungrowModbusPowerFlowBinarySensor(CoordinatorEntity[SungrowPlantCoordinator], BinarySensorEntity):
    """A single bit of ``power_flow_status`` exposed as an on/off binary sensor (#326).

    Decoded from wire register 13000, one entity per surfaced bit. This turns
    the raw bitfield sensor into automation-friendly triggers like
    ``binary_sensor.battery_charging`` and ``binary_sensor.exporting_power`` so
    users don't have to write template sensors to make the same decisions the
    inverter is already publishing.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        device: dict[str, Any],
        bit_index: int,
        translation_key: str,
    ) -> None:
        """Initialize a power-flow bit sensor for a local Modbus inverter."""
        super().__init__(coordinator)
        self.device_uuid = str(device["uuid"])
        self._bit_index = bit_index
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_power_flow_{translation_key}"
        device_class = _POWER_FLOW_DEVICE_CLASSES.get(bit_index)
        if device_class is not None:
            self._attr_device_class = device_class
        local_url = getattr(coordinator, "local_configuration_url", None)
        self._attr_device_info = build_device_info(
            device,
            coordinator.plant_id,
            fallback_name=coordinator.plant_name,
            via_plant_id=getattr(coordinator, "via_plant_id", None),
            configuration_url=local_url if isinstance(local_url, str) else None,
        )

    def _raw(self) -> int | None:
        """Return the current ``power_flow_status`` register value, or None if missing."""
        data = self.coordinator.data or {}
        point = data.get("power_flow_status")
        if not isinstance(point, dict):
            return None
        val = point.get("value")
        if val is None:
            return None
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Unavailable if the last poll failed or ``power_flow_status`` isn't reported."""
        return super().available and self._raw() is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether this power-flow bit is set."""
        raw = self._raw()
        if raw is None:
            return None
        return bool(raw & (1 << self._bit_index))
