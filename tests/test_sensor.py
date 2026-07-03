"""Tests for the Sungrow sensor platform."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import SungrowData
from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.sensor import (
    SungrowDeviceSensor,
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

    @pytest.mark.parametrize(
        ("code", "unit", "name", "device_class"),
        [
            ("battery_level", "%", "Battery Level", SensorDeviceClass.BATTERY),
            ("battery_soh", "%", "Battery Health (SOH)", SensorDeviceClass.BATTERY),
            ("battery_total_charge_energy", "Wh", "Battery Total Charge Energy", SensorDeviceClass.ENERGY),
            ("meter_forward_active_energy", "Wh", "Meter Forward Active Energy", SensorDeviceClass.ENERGY),
            ("meter_active_power", "W", "Meter Active Power", SensorDeviceClass.POWER),
            ("ev_charger_max_power", "kW", "EV Charger Max Power", SensorDeviceClass.POWER),
            ("ev_charger_status", "", "EV Charger Status", None),
        ],
    )
    def test_documented_measure_point_aliases(self, code, unit, name, device_class):
        """Documented measure-point codes get a friendly name; class is inferred from the unit."""
        coordinator = self._make_coordinator()
        sensor = SungrowSensor(coordinator, code, "123", "Plant", {"code": code, "value": "1", "unit": unit})

        assert sensor._attr_name == name
        assert sensor._attr_device_class == device_class

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

    def test_sensor_icon_fallback_for_unclassified(self):
        """Sensors with no recognised device class fall back to the solar icon."""
        coordinator = self._make_coordinator()
        init_data = {"code": "x", "value": "1", "unit": "", "name": "X"}
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", init_data)

        assert sensor._attr_icon == "mdi:solar-power-variant"

    def test_sensor_icon_none_for_classified(self):
        """Sensors with a known device class use None so HA picks the right icon."""
        coordinator = self._make_coordinator()
        # Battery SoC — device_class: BATTERY → should show mdi:battery automatically
        init_data = {"code": "battery_level_soc", "value": "80", "unit": "%"}
        sensor = SungrowSensor(coordinator, "battery_level_soc", "123", "Plant", init_data)

        assert sensor._attr_icon is None
        assert sensor._attr_device_class == SensorDeviceClass.BATTERY

    def test_sensor_alias_battery_soc(self):
        """battery_level_soc gets a friendly human-readable alias."""
        coordinator = self._make_coordinator()
        init_data = {"code": "battery_level_soc", "value": "80", "unit": "%"}
        sensor = SungrowSensor(coordinator, "battery_level_soc", "123", "Plant", init_data)

        assert sensor._attr_name == "Battery State of Charge"

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

    def test_native_value_unclassified_numeric_stays_string(self):
        """A numeric-looking status point without a class is not coerced to a float."""
        data = {"status": {"code": "status", "value": "1", "unit": "", "name": "Status"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "status", "123", "Plant", data["status"])

        # No unit -> no device/state class -> left as a string, not 1.0.
        assert sensor.native_value == "1"
        assert isinstance(sensor.native_value, str)

    def test_no_raw_extra_state_attributes(self):
        """The raw API point payload is not exposed as state attributes (recorder bloat)."""
        point_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        coordinator = self._make_coordinator({"power": point_data})
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", point_data)

        assert sensor.extra_state_attributes is None


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
    entry.runtime_data = SungrowData(coordinators=coordinators, control=MagicMock(), devices={})

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
    entry.runtime_data = SungrowData(coordinators=coordinators, control=MagicMock(), devices={})

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []


# ---------------------------------------------------------------------------
# Per-device sensors (issue #74)
# ---------------------------------------------------------------------------


async def test_device_sensors_created_when_enabled(hass: HomeAssistant):
    """Device points not already at plant level become sensors under their device."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    coordinator.enable_device_sensors = True
    # The platform reads the live device list from the coordinator for naming.
    coordinator.devices = [{"uuid": "chg-1", "device_name": "AC011E", "device_type": 999}]
    coordinator.device_data = {
        "chg-1": {
            "ev_charger_power": {"code": "ev_charger_power", "value": "7.2", "unit": "kW"},
            # Duplicate of a plant-level point -> should be skipped.
            "total_active_power": {"code": "total_active_power", "value": "5.0", "unit": "kW"},
        }
    }
    entry.runtime_data = SungrowData(
        coordinators=[coordinator],
        control=MagicMock(),
        devices={"12345": coordinator.devices},
    )

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    device_sensors = [e for e in added if isinstance(e, SungrowDeviceSensor)]
    assert len(device_sensors) == 1  # the plant-duplicate point was skipped
    sensor = device_sensors[0]
    assert sensor.point_code == "ev_charger_power"
    assert sensor.device_uuid == "chg-1"
    assert sensor._attr_unique_id == "12345_chg-1_ev_charger_power"
    # Name is derived from the coordinator's device metadata.
    assert sensor._attr_device_info["name"] == "AC011E"
    assert (DOMAIN, "chg-1") in sensor._attr_device_info["identifiers"]
    assert sensor._attr_device_info["via_device"] == (DOMAIN, "12345")
    # Reads its value from the coordinator's per-device data.
    assert sensor.native_value == 7.2


async def test_sensor_dynamic_add_on_new_point(hass: HomeAssistant):
    """A plant point that appears after setup is added at runtime (dynamic-devices)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    listeners: list = []
    coordinator.async_add_listener = lambda cb, *a: listeners.append(cb) or (lambda: None)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert len(added) == 1

    # A new point appears on a later poll; the coordinator notifies its listeners.
    coordinator.data = {**coordinator.data, "daily_energy": {"value": "12.0", "unit": "kWh"}}
    for cb in listeners:
        cb()
    assert len(added) == 2
    assert any(e.point_code == "daily_energy" for e in added)

    # Firing again with nothing new must not re-add existing sensors.
    for cb in listeners:
        cb()
    assert len(added) == 2


async def test_device_sensors_not_created_when_disabled(hass: HomeAssistant):
    """No device sensors are created when the option is off, even with device data present."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    coordinator.enable_device_sensors = False
    coordinator.device_data = {"chg-1": {"ev_charger_power": {"value": "7.2"}}}
    entry.runtime_data = SungrowData(
        coordinators=[coordinator],
        control=MagicMock(),
        devices={"12345": [{"uuid": "chg-1", "device_name": "AC011E"}]},
    )

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert not any(isinstance(e, SungrowDeviceSensor) for e in added)
    # Plant sensors are still created.
    assert any(isinstance(e, SungrowSensor) for e in added)
