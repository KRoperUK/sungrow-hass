"""Tests for the Sungrow sensor platform."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.sensor import (
    SungrowSensor,
    async_setup_entry,
    infer_device_class,
)

from .conftest import MOCK_CONFIG_DATA

# ---------------------------------------------------------------------------
# infer_device_class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "code", "device_class", "state_class"),
    [
        ("kW", "power", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
        ("W", "power", SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT),
        ("kWh", "energy", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
        ("Wh", "energy", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
        ("MWh", "energy", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
        ("V", "voltage", SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT),
        ("A", "current", SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT),
        ("Hz", "freq", SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT),
        ("°C", "temp", SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
        ("kvar", "reactive", SensorDeviceClass.REACTIVE_POWER, SensorStateClass.MEASUREMENT),
        # Case-insensitive matching.
        ("kwh", "energy", SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING),
        # Battery percentage disambiguated by the code name.
        ("%", "battery_soc", SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT),
        # Generic percentage gets a state class but no device class.
        ("%", "efficiency", None, SensorStateClass.MEASUREMENT),
        # Unknown / empty units.
        ("", "status", None, None),
        (None, "status", None, None),
        ("widgets", "x", None, None),
    ],
)
def test_infer_device_class(unit, code, device_class, state_class):
    """Units map to the expected device and state classes (issue #19)."""
    assert infer_device_class(unit, code) == (device_class, state_class)


# ---------------------------------------------------------------------------
# SungrowSensor unit tests
# ---------------------------------------------------------------------------


class TestSungrowSensor:
    """Unit tests for SungrowSensor entity."""

    def _make_coordinator(self, data=None):
        coordinator = MagicMock()
        coordinator.data = data or {}
        return coordinator

    def test_sensor_name_from_code(self):
        """Test sensor name is derived from the point code."""
        coordinator = self._make_coordinator()
        init_data = {"code": "total_active_power", "value": "5.0", "unit": "kW", "name": "Total"}
        sensor = SungrowSensor(coordinator, "total_active_power", "123", "My Plant", init_data)

        assert sensor._attr_name == "Total Active Power"
        assert sensor._attr_unique_id == "123_total_active_power"

    def test_sensor_alias_for_battery_power(self):
        """Known opaque battery codes get a friendly alias."""
        coordinator = self._make_coordinator()
        init_data = {"code": "total_field_energy_storage_active_power", "value": "-1.4", "unit": "kW"}
        sensor = SungrowSensor(coordinator, "total_field_energy_storage_active_power", "123", "My Plant", init_data)

        assert sensor._attr_name == "Battery Power"

    def test_sensor_alias_for_extra_measure_point(self):
        """User-configured extra measure points use documented aliases when available."""
        coordinator = self._make_coordinator()
        init_data = {"code": "battery_charge_power", "value": "1500", "unit": "W"}
        sensor = SungrowSensor(coordinator, "battery_charge_power", "123", "My Plant", init_data)

        assert sensor._attr_name == "Battery Charge Power"

    def test_sensor_name_numeric_code_fallback(self):
        """Test sensor with a numeric code falls back to init_data name."""
        coordinator = self._make_coordinator()
        init_data = {"code": "12345", "value": "99", "unit": "W", "name": "Some Sensor"}
        sensor = SungrowSensor(coordinator, "12345", "123", "My Plant", init_data)

        assert sensor._attr_name == "Some Sensor"

    def test_sensor_device_class_power(self):
        """Test kW unit infers POWER device class."""
        coordinator = self._make_coordinator()
        init_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", init_data)

        assert sensor._attr_device_class == SensorDeviceClass.POWER
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_sensor_device_class_energy(self):
        """Test kWh unit infers ENERGY device class for the Energy dashboard."""
        coordinator = self._make_coordinator()
        init_data = {"code": "energy", "value": "12.0", "unit": "kWh", "name": "Energy"}
        sensor = SungrowSensor(coordinator, "energy", "123", "Plant", init_data)

        assert sensor._attr_device_class == SensorDeviceClass.ENERGY
        assert sensor._attr_state_class == SensorStateClass.TOTAL_INCREASING
        assert sensor._attr_native_unit_of_measurement == "kWh"

    def test_sensor_device_class_unknown_unit(self):
        """Test unknown unit doesn't set a device class."""
        coordinator = self._make_coordinator()
        init_data = {"code": "status", "value": "OK", "unit": "", "name": "Status"}
        sensor = SungrowSensor(coordinator, "status", "123", "Plant", init_data)

        assert sensor._attr_device_class is None

    def test_sensor_icon(self):
        """Test all sensors use the solar icon."""
        coordinator = self._make_coordinator()
        init_data = {"code": "x", "value": "1", "unit": "", "name": "X"}
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", init_data)

        assert sensor._attr_icon == "mdi:solar-power-variant"

    def test_sensor_device_info(self):
        """Test sensor has device_info grouping it under its plant."""
        coordinator = self._make_coordinator()
        init_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        sensor = SungrowSensor(coordinator, "power", "456", "My Solar Plant", init_data)

        assert sensor._attr_device_info is not None
        assert sensor._attr_device_info["identifiers"] == {("sungrow", "456")}
        assert sensor._attr_device_info["name"] == "My Solar Plant"
        assert sensor._attr_device_info["manufacturer"] == "Sungrow"

    def test_sensor_disabled_by_default(self):
        """Test sensors with no value are disabled by default."""
        coordinator = self._make_coordinator()

        s1 = SungrowSensor(coordinator, "x", "123", "Plant", {"value": None})
        assert s1.entity_registry_enabled_default is False

        s2 = SungrowSensor(coordinator, "y", "123", "Plant", {"value": "  "})
        assert s2.entity_registry_enabled_default is False

        s3 = SungrowSensor(coordinator, "z", "123", "Plant", {"value": "Unknown"})
        assert s3.entity_registry_enabled_default is False

        s4 = SungrowSensor(coordinator, "v", "123", "Plant", {"value": "1.2"})
        assert s4.entity_registry_enabled_default is True

    def test_native_value_float_conversion(self):
        """Test native_value converts string numbers to float."""
        data = {"power": {"code": "power", "value": "5.23", "unit": "kW", "name": "Power"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", data["power"])

        assert sensor.native_value == 5.23

    def test_native_value_non_numeric(self):
        """Test native_value returns raw string for non-numeric values."""
        data = {"status": {"code": "status", "value": "Running", "unit": "", "name": "Status"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "status", "123", "Plant", data["status"])

        assert sensor.native_value == "Running"

    def test_native_value_none_when_missing(self):
        """Test native_value returns None when data is missing."""
        coordinator = self._make_coordinator({})
        sensor = SungrowSensor(coordinator, "missing", "123", "Plant", {"value": "0"})

        assert sensor.native_value is None

    def test_native_value_none_when_coordinator_data_none(self):
        """Test native_value returns None when coordinator.data is None."""
        coordinator = self._make_coordinator(None)
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", {"value": "0"})

        assert sensor.native_value is None

    def test_extra_state_attributes(self):
        """Test extra_state_attributes returns the full data point dict."""
        point_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        coordinator = self._make_coordinator({"power": point_data})
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", point_data)

        assert sensor.extra_state_attributes == point_data

    def test_extra_state_attributes_empty_when_missing(self):
        """Test extra_state_attributes returns {} when data is missing."""
        coordinator = self._make_coordinator({})
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", {"value": "0"})

        assert sensor.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# async_setup_entry (platform) — builds entities from stored coordinators
# ---------------------------------------------------------------------------


def _coordinator_with(plant_id, plant_name, data):
    coordinator = MagicMock()
    coordinator.plant_id = plant_id
    coordinator.plant_name = plant_name
    coordinator.data = data
    return coordinator


async def test_sensor_setup_creates_entities(hass: HomeAssistant):
    """The platform creates a sensor per data point across all coordinators."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinators = [
        _coordinator_with(
            "12345",
            "Plant A",
            {
                "total_active_power": {"value": "5.0", "unit": "kW", "name": "Total Active Power"},
                "daily_energy": {"value": "12.0", "unit": "kWh", "name": "Daily Energy"},
            },
        ),
        _coordinator_with("67890", "Plant B", {"total_active_power": {"value": "3.1", "unit": "kW"}}),
    ]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinators": coordinators,
        "control": MagicMock(),
        "devices": {},
        "heartbeat_stop": {},
    }

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == 3
    names = [e._attr_name for e in added]
    assert names.count("Total Active Power") == 2


async def test_sensor_setup_skips_plant_with_no_data(hass: HomeAssistant):
    """Coordinators with no data don't produce entities."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinators = [_coordinator_with("12345", "Plant A", {}), _coordinator_with("67890", "Plant B", {})]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinators": coordinators,
        "control": MagicMock(),
        "devices": {},
        "heartbeat_stop": {},
    }

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []
