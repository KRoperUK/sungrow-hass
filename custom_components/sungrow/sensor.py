from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SungrowPlantCoordinator

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow sensors from the coordinators created during entry setup."""
    coordinators: list[SungrowPlantCoordinator] = hass.data[DOMAIN][entry.entry_id]

    entities: list[SungrowSensor] = []
    for coordinator in coordinators:
        if not coordinator.data:
            _LOGGER.warning("No data received for plant %s", coordinator.plant_name)
            continue

        # The data structure is { "P_CODE": { "code": ..., "value": ..., "unit": ..., "name": ... } }
        for point_code, point_data in coordinator.data.items():
            entities.append(
                SungrowSensor(coordinator, point_code, coordinator.plant_id, coordinator.plant_name, point_data)
            )

    async_add_entities(entities)


class SungrowSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Sungrow Sensor."""

    has_entity_name = True

    def __init__(self, coordinator, point_code, plant_id, plant_name, init_data):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.point_code = point_code
        self.plant_id = plant_id

        # Prefer generating name from code to avoid Chinese names from API
        # The API often returns Chinese names even when locale is set to English
        # We assume point_code is a readable string identifier (e.g. 'total_active_power')
        if point_code.isdigit():
            # Fallback if we only have a number, but ideally we should have a string key
            sensor_name = init_data.get("name", f"Sensor {point_code}")
        else:
            sensor_name = point_code.replace("_", " ").title()

        # With has_entity_name = True, HA prefixes the device name automatically
        self._attr_name = sensor_name
        _LOGGER.debug("Created sensor: %s %s (code: %s)", plant_name, sensor_name, point_code)
        self._attr_unique_id = f"{plant_id}_{point_code}"
        self._attr_icon = "mdi:solar-power-variant"

        # Group sensors under a device per plant
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant_id)},
            name=plant_name,
            manufacturer="Sungrow",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://isolarcloud.eu",
        )

        # Programmatically hide sensors that are "Unknown" at first setup
        # This prevents UI clutter for unsupported attributes (e.g. meters/batteries not present)
        initial_value = init_data.get("value")
        if initial_value is None or str(initial_value).strip() == "" or str(initial_value).lower() == "unknown":
            self._attr_entity_registry_enabled_default = False

        # Infer device class / state class / unit so the Energy dashboard and history
        # graphs work out of the box (issue #19).
        self._attr_native_unit_of_measurement = init_data.get("unit")
        device_class, state_class = infer_device_class(init_data.get("unit"), point_code)
        self._attr_device_class = device_class
        self._attr_state_class = state_class

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.coordinator.data and self.point_code in self.coordinator.data:
            val = self.coordinator.data[self.point_code].get("value")
            # Try convert to float if it looks like a number but is string
            try:
                return float(val)
            except (ValueError, TypeError):
                return val
        return None

    @property
    def extra_state_attributes(self):
        """Return attributes."""
        if self.coordinator.data and self.point_code in self.coordinator.data:
            return self.coordinator.data[self.point_code]
        return {}
