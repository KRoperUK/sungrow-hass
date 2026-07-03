from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_GATEWAY, DEFAULT_CONSOLE_URL, DOMAIN, GATEWAY_CONSOLE_URLS
from .coordinator import SungrowPlantCoordinator

# Sensors are read-only and updated in bulk by the coordinator, so no per-entity
# write parallelism limit is needed.
PARALLEL_UPDATES = 0

# Friendly names for point codes that are otherwise opaque or overly generic.
# These are applied in addition to the code-based naming, so existing entities
# keep their unique_id while users see a clearer label.
SENSOR_ALIASES: dict[str, str] = {
    "total_field_energy_storage_active_power": "Battery Power",
    "total_field_energy_storage_maximum_reactive_power": "Battery Max Reactive Power",
    "total_field_chargeable_energy": "Battery Chargeable Energy",
    "total_field_dischargeable_energy": "Battery Dischargeable Energy",
    "total_field_maximum_rechargeable_power": "Battery Max Charge Power",
    "total_field_maximum_dischargeable_power": "Battery Max Discharge Power",
    "daily_field_charge_capacity": "Battery Daily Charge Capacity",
    "daily_field_discharge_capacity": "Battery Daily Discharge Capacity",
    "energy_storage_active_power_ems": "EMS Battery Power",
    "energy_storage_soc_ems": "EMS Battery SOC",
    "battery_level_soc": "Battery State of Charge",
}

# Per-issue custom codes that users commonly request. If the point_id is configured
# via the options flow, the code is used as-is; we also supply a friendly alias here
# so the UI label is meaningful.
EXTRA_CODE_ALIASES: dict[str, str] = {
    "battery_charge_power": "Battery Charge Power",
    "battery_discharge_power": "Battery Discharge Power",
    "ev_charger_power": "EV Charger Power",
    "ev_charger_energy": "EV Charger Energy",
}

_LOGGER = logging.getLogger(__name__)

# Map a (lower-cased) unit of measurement to the appropriate device and state class.
# Energy uses TOTAL_INCREASING so cumulative counters (and daily counters that reset)
# are accepted by the Home Assistant Energy dashboard. See issue #19.
MEASUREMENT = SensorStateClass.MEASUREMENT
TOTAL_INCREASING = SensorStateClass.TOTAL_INCREASING

_UNIT_CLASS_MAP: dict[str, tuple[SensorDeviceClass, SensorStateClass]] = {
    # Power
    "w": (SensorDeviceClass.POWER, MEASUREMENT),
    "kw": (SensorDeviceClass.POWER, MEASUREMENT),
    "mw": (SensorDeviceClass.POWER, MEASUREMENT),
    "gw": (SensorDeviceClass.POWER, MEASUREMENT),
    # Energy
    "wh": (SensorDeviceClass.ENERGY, TOTAL_INCREASING),
    "kwh": (SensorDeviceClass.ENERGY, TOTAL_INCREASING),
    "mwh": (SensorDeviceClass.ENERGY, TOTAL_INCREASING),
    "gwh": (SensorDeviceClass.ENERGY, TOTAL_INCREASING),
    # Voltage
    "v": (SensorDeviceClass.VOLTAGE, MEASUREMENT),
    "mv": (SensorDeviceClass.VOLTAGE, MEASUREMENT),
    "kv": (SensorDeviceClass.VOLTAGE, MEASUREMENT),
    # Current
    "a": (SensorDeviceClass.CURRENT, MEASUREMENT),
    "ma": (SensorDeviceClass.CURRENT, MEASUREMENT),
    # Frequency
    "hz": (SensorDeviceClass.FREQUENCY, MEASUREMENT),
    # Temperature
    "°c": (SensorDeviceClass.TEMPERATURE, MEASUREMENT),
    "℃": (SensorDeviceClass.TEMPERATURE, MEASUREMENT),
    "c": (SensorDeviceClass.TEMPERATURE, MEASUREMENT),
    "°f": (SensorDeviceClass.TEMPERATURE, MEASUREMENT),
    # Reactive / apparent power
    "var": (SensorDeviceClass.REACTIVE_POWER, MEASUREMENT),
    "kvar": (SensorDeviceClass.REACTIVE_POWER, MEASUREMENT),
    "va": (SensorDeviceClass.APPARENT_POWER, MEASUREMENT),
    "kva": (SensorDeviceClass.APPARENT_POWER, MEASUREMENT),
}

# Units that need the entity code inspected to disambiguate the device class.
_PERCENT_BATTERY_HINTS = ("soc", "battery", "capacity", "charge")


def infer_device_class(unit: str | None, point_code: str) -> tuple[SensorDeviceClass | None, SensorStateClass | None]:
    """Infer a device class and state class from a unit of measurement.

    Returns ``(None, None)`` when the unit is unknown so the sensor is still created
    as a plain numeric/text sensor.
    """
    if not unit:
        return None, None

    key = unit.strip().lower()

    if key in _UNIT_CLASS_MAP:
        return _UNIT_CLASS_MAP[key]

    if key in ("%", "percent"):
        code = point_code.lower()
        if any(hint in code for hint in _PERCENT_BATTERY_HINTS):
            return SensorDeviceClass.BATTERY, MEASUREMENT
        # Generic percentage (e.g. efficiency) — measurement only.
        return None, MEASUREMENT

    return None, None


def _build_sensors(coordinator: SungrowPlantCoordinator, console_url: str) -> list[SungrowSensor]:
    """Build the full set of sensors the coordinator currently warrants.

    Called both at setup and on every coordinator update, so points/devices that
    appear after setup are surfaced at runtime (dynamic-devices). The caller
    de-duplicates by unique_id, so returning the complete set each time is fine.
    """
    sensors: list[SungrowSensor] = []

    # Plant-level sensors.
    # The data structure is { "P_CODE": { "code": ..., "value": ..., "unit": ..., "name": ... } }
    if coordinator.data:
        for point_code, point_data in coordinator.data.items():
            sensors.append(
                SungrowSensor(
                    coordinator, point_code, coordinator.plant_id, coordinator.plant_name, point_data, console_url
                )
            )

    # Per-device sensors (opt-in, issue #74): surface points reported per device
    # (EV chargers, meters, extra batteries). Points already present at the plant
    # level are skipped so enabling this doesn't just duplicate every plant sensor
    # under the inverter.
    if coordinator.enable_device_sensors and coordinator.device_data:
        plant_codes = set(coordinator.data or {})
        device_names = {
            str(d.get("uuid")): (d.get("device_name") or d.get("device_model_name") or coordinator.plant_name)
            for d in coordinator.devices
            if d.get("uuid")
        }
        for uuid, points in coordinator.device_data.items():
            device_name = device_names.get(uuid, f"Device {uuid}")
            for point_code, point_data in points.items():
                if point_code in plant_codes:
                    continue
                sensors.append(
                    SungrowDeviceSensor(coordinator, point_code, coordinator.plant_id, uuid, device_name, point_data)
                )

    return sensors


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow sensors from the coordinators created during entry setup."""
    coordinators = entry.runtime_data.coordinators
    # Point the device "Visit" link at the region's iSolarCloud web console.
    console_url = GATEWAY_CONSOLE_URLS.get(entry.data.get(CONF_GATEWAY, ""), DEFAULT_CONSOLE_URL)

    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        """Add entities for any plant points / devices not seen yet."""
        new_entities: list[SungrowSensor] = []
        for coordinator in coordinators:
            for entity in _build_sensors(coordinator, console_url):
                uid = entity.unique_id
                if uid is None or uid in known_unique_ids:
                    continue
                known_unique_ids.add(uid)
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    # Add the initial set, then keep watching each coordinator for new devices.
    _add_new_entities()
    for coordinator in coordinators:
        entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class SungrowSensor(CoordinatorEntity, SensorEntity):
    """Representation of a plant-level Sungrow sensor."""

    has_entity_name = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        point_code: str,
        plant_id: str,
        plant_name: str,
        init_data: dict[str, Any],
        console_url: str = DEFAULT_CONSOLE_URL,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.point_code = point_code
        self.plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{point_code}"

        # Group sensors under a device per plant.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant_id)},
            name=plant_name,
            manufacturer="Sungrow",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=console_url,
        )
        self._apply_point_metadata(point_code, init_data, plant_name)

    def _apply_point_metadata(self, point_code: str, init_data: dict[str, Any], label: str) -> None:
        """Set the name, unit and device/state class from a point payload.

        Shared by plant-level and per-device sensors so both name and classify points
        the same way.
        """
        # Prefer generating the name from the code to avoid Chinese names from the API
        # (the API often returns Chinese names even when locale is English). We assume
        # point_code is a readable string identifier (e.g. 'total_active_power').
        if point_code.isdigit():
            sensor_name = init_data.get("name", f"Sensor {point_code}")
        else:
            sensor_name = point_code.replace("_", " ").title()
        # Apply friendly aliases for known opaque / user-configured extra codes.
        sensor_name = EXTRA_CODE_ALIASES.get(point_code, SENSOR_ALIASES.get(point_code, sensor_name))

        # With has_entity_name = True, HA prefixes the device name automatically.
        self._attr_name = sensor_name
        _LOGGER.debug("Created sensor: %s %s (code: %s)", label, sensor_name, point_code)

        # Hide points that are "Unknown" at first setup to reduce UI clutter
        # (e.g. meters/batteries not present).
        initial_value = init_data.get("value")
        if initial_value is None or str(initial_value).strip() == "" or str(initial_value).lower() == "unknown":
            self._attr_entity_registry_enabled_default = False

        # Infer device class / state class / unit so the Energy dashboard and history
        # graphs work out of the box (issue #19).
        unit = init_data.get("unit")
        self._attr_native_unit_of_measurement = unit if unit else None
        device_class, state_class = infer_device_class(init_data.get("unit"), point_code)
        self._attr_device_class = device_class
        self._attr_state_class = state_class

        # Let HA choose the icon for sensors with a known device class; fall back to
        # the solar panel icon only for unclassified sensors.
        self._attr_icon = None if device_class else "mdi:solar-power-variant"

    def _current_point(self) -> dict[str, Any] | None:
        """Return the current point payload for this sensor (plant-level source)."""
        data = self.coordinator.data
        if data and self.point_code in data:
            point: dict[str, Any] = data[self.point_code]
            return point
        return None

    @property
    def native_value(self) -> float | str | None:
        """Return the state of the sensor."""
        point = self._current_point()
        if point is None:
            return None
        val: Any = point.get("value")
        if val is None:
            return None
        # Only coerce to a number for sensors classified as numeric (a device or
        # state class). Unclassified text/status points are left as strings so a
        # status like "1" or a boolean isn't silently turned into a float.
        if self._attr_device_class is None and self._attr_state_class is None:
            return str(val)
        try:
            return float(val)
        except (ValueError, TypeError):
            # point payload values are untyped (Any) upstream.
            return cast("str | None", val)


class SungrowDeviceSensor(SungrowSensor):
    """A sensor for a specific device (EV charger, meter, extra battery) under a plant.

    Reads from the coordinator's per-device realtime data and is grouped under its own
    device, linked to the plant device via ``via_device`` (issue #74).
    """

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        point_code: str,
        plant_id: str,
        device_uuid: str,
        device_name: str,
        init_data: dict[str, Any],
    ) -> None:
        """Initialize a device-scoped sensor."""
        # Skip SungrowSensor.__init__ (it builds the plant device); reuse the shared
        # metadata helper but with a device-scoped identity and data source.
        CoordinatorEntity.__init__(self, coordinator)
        self.point_code = point_code
        self.plant_id = plant_id
        self.device_uuid = device_uuid
        self._attr_unique_id = f"{plant_id}_{device_uuid}_{point_code}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            name=device_name,
            manufacturer="Sungrow",
            via_device=(DOMAIN, plant_id),
        )
        self._apply_point_metadata(point_code, init_data, device_name)

    def _current_point(self) -> dict[str, Any] | None:
        """Return the current point payload from the coordinator's per-device data."""
        device_data = getattr(self.coordinator, "device_data", None) or {}
        point: dict[str, Any] | None = device_data.get(self.device_uuid, {}).get(self.point_code)
        return point
