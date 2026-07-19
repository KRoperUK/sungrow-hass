"""Tests for the Sungrow sensor platform."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pysolarcloud.plants import DeviceType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import SungrowData
from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.sensor import (
    PLANT_DETAIL_SENSORS,
    SungrowDeviceSensor,
    SungrowPlantDetailSensor,
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
        # Dimensionless integer tallies graph as a measurement, not text.
        ("", "afci_fault_count", None, SensorStateClass.MEASUREMENT),
        # Unknown / empty units.
        ("", "status", None, None),
        (None, "status", None, None),
        ("widgets", "x", None, None),
    ],
)
def test_infer_device_class(unit, code, device_class, state_class):
    """Units map to the expected device and state classes (issue #19)."""
    assert infer_device_class(unit, code) == (device_class, state_class)


def test_infer_device_class_power_factor_no_unit():
    """A dimensionless power-factor code classifies via the code (issue #105)."""
    assert infer_device_class("", "meter_power_factor", "0") == (
        SensorDeviceClass.POWER_FACTOR,
        SensorStateClass.MEASUREMENT,
    )


# ---------------------------------------------------------------------------
# Per-device re-homing (#158)
# ---------------------------------------------------------------------------


def _coord_with_devices(devices, data=None):
    coordinator = MagicMock()
    coordinator.data = data or {}
    coordinator.devices = devices
    coordinator.plant_name = "Plant"
    coordinator.plants_service = MagicMock()  # cloud-backed coordinator
    coordinator.via_plant_id = None
    return coordinator


def test_signal_strength_sensor_gets_db_unit_and_signal_icon():
    """WLAN/wireless signal strength classifies as SIGNAL_STRENGTH with a dB unit + signal icon."""
    point = {"code": "wlan_signal_strength", "value": "-62", "unit": ""}
    coordinator = _coord_with_devices([], data={"wlan_signal_strength": point})
    sensor = SungrowSensor(coordinator, "wlan_signal_strength", "123", "Plant", point)
    assert sensor._attr_device_class == SensorDeviceClass.SIGNAL_STRENGTH
    assert sensor._attr_native_unit_of_measurement == "dB"
    assert sensor._attr_icon == "mdi:signal"
    assert sensor.native_value == -62.0


@pytest.mark.parametrize(
    "code",
    [
        "battery_soc",
        "battery_level_soc",
        "battery_level",
        "battery_state_of_charge",
        "total_field_soc",
        "energy_storage_soc_ems",
    ],
)
def test_battery_soc_unitless_gets_percent_unit(code):
    """Unitless SOC points still get device_class battery with unit % (#228).

    The API often omits the unit for charge-level points; classification still
    marks them BATTERY from the code, but HA rejects battery without '%'.
    """
    point = {"code": code, "value": "72", "unit": None}
    coordinator = _coord_with_devices([], data={code: point})
    sensor = SungrowSensor(coordinator, code, "123", "Plant", point)
    assert sensor._attr_device_class == SensorDeviceClass.BATTERY
    assert sensor._attr_native_unit_of_measurement == "%"
    assert sensor.native_value == 72.0


def test_battery_soc_empty_string_unit_gets_percent():
    """An empty-string unit from the API is treated like missing (#228)."""
    point = {"code": "battery_soc", "value": "55", "unit": ""}
    coordinator = _coord_with_devices([], data={"battery_soc": point})
    sensor = SungrowSensor(coordinator, "battery_soc", "123", "Plant", point)
    assert sensor._attr_device_class == SensorDeviceClass.BATTERY
    assert sensor._attr_native_unit_of_measurement == "%"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.95", 95.0),  # SBH250-V11 plant returns a 0–1 fraction (#314)
        ("0.5", 50.0),
        ("1.0", 100.0),  # exactly 1.0 stays in the fraction bracket -> full battery
        ("0", 0.0),  # empty battery, no scaling either way
    ],
)
def test_battery_soc_fraction_scaled_to_percent(raw, expected):
    """SOC reported as a 0–1 fraction is rescaled to a real percentage (#314).

    Sungrow's iSolarCloud returns ``battery_level_soc`` (83252) as a 0–1 fraction on
    some plants (SBH250-V11) instead of the usual 0–100. Because we already force the
    unit to ``%`` for BATTERY sensors (#228), the raw fraction otherwise renders as
    ``0.95 %``.
    """
    point = {"id": "83252", "code": "battery_level_soc", "value": raw, "unit": ""}
    coordinator = _coord_with_devices([], data={"battery_level_soc": point})
    sensor = SungrowSensor(coordinator, "battery_level_soc", "123", "Plant", point)
    assert sensor._attr_device_class == SensorDeviceClass.BATTERY
    assert sensor._attr_native_unit_of_measurement == "%"
    assert sensor.native_value == expected


@pytest.mark.parametrize("raw, expected", [("95", 95.0), ("100", 100.0), ("42.5", 42.5)])
def test_battery_soc_percent_form_unchanged(raw, expected):
    """SOC already in 0–100 percent form (>1) is passed through untouched (#314)."""
    point = {"id": "83252", "code": "battery_level_soc", "value": raw, "unit": "%"}
    coordinator = _coord_with_devices([], data={"battery_level_soc": point})
    sensor = SungrowSensor(coordinator, "battery_level_soc", "123", "Plant", point)
    assert sensor.native_value == expected


def test_power_fraction_gets_gauge_icon():
    """A per-code override gives power_fraction a gauge icon, not the solar fallback."""
    point = {"code": "power_fraction", "value": "0.83", "unit": ""}
    coordinator = _coord_with_devices([], data={"power_fraction": point})
    sensor = SungrowSensor(coordinator, "power_fraction", "123", "Plant", point)
    assert sensor._attr_icon == "mdi:gauge"


def test_sensor_exposes_transport_source_when_present():
    """A point that went through the Modbus merge exposes its source (#159)."""
    point = {"code": "total_active_power", "value": "256", "unit": "W", "source": "modbus"}
    coordinator = _coord_with_devices([], data={"total_active_power": point})
    sensor = SungrowSensor(coordinator, "total_active_power", "123", "Plant", point)
    assert sensor.extra_state_attributes == {"source": "modbus"}


def test_sensor_no_attributes_without_source():
    """Cloud-only points carry no source, so no state attributes are emitted (no bloat)."""
    point = {"code": "total_active_power", "value": "256", "unit": "W"}
    coordinator = _coord_with_devices([], data={"total_active_power": point})
    sensor = SungrowSensor(coordinator, "total_active_power", "123", "Plant", point)
    assert sensor.extra_state_attributes is None


def test_device_type_code_is_diagnostic():
    """device_type_code is a Modbus map selector — keep it out of the main entity list."""
    point = {"code": "device_type_code", "value": "9732", "unit": None, "source": "modbus"}
    coordinator = _coord_with_devices([], data={"device_type_code": point})
    sensor = SungrowSensor(coordinator, "device_type_code", "123", "Plant", point)
    assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC


def test_daily_yield_sensor_surfaces_modbus_diagnostic_when_captured():
    """Opt-in daily_yield diagnostic dump rides along on the sensor as an extra attribute."""
    point = {"code": "daily_yield", "value": "64.0", "unit": "kWh", "source": "modbus"}
    coordinator = _coord_with_devices([], data={"daily_yield": point})
    coordinator.daily_yield_diagnostic = {
        "start": 4999,
        "raw": {"4999": 9732, "5002": 640, "5003": 6330},
        "candidates": [{"address": 5002, "raw": 640, "scale": 0.1, "unit": "kWh", "value": 64.0}],
        "current_mapping": {"address": 5002, "raw": 640, "scale": 0.1, "unit": "kWh"},
    }
    sensor = SungrowSensor(coordinator, "daily_yield", "123", "Plant", point)
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["source"] == "modbus"
    assert attrs["daily_yield_diagnostic"]["raw"]["5002"] == 640
    # The current-mapping entry echoes the existing decode so a user eyeballing the
    # attribute can verify which (address, scale) their live entity's value came from.
    assert attrs["daily_yield_diagnostic"]["current_mapping"]["raw"] == 640
    # And the candidate list lets them spot the off-by-one or wrong-scale option.
    assert attrs["daily_yield_diagnostic"]["candidates"][0]["value"] == 64.0


def test_daily_yield_sensor_no_diagnostic_when_not_captured():
    """No diagnostic attribute when the coordinator hasn't captured one yet (cloud-only path)."""
    point = {"code": "daily_yield", "value": "25.2", "unit": "kWh", "source": "cloud"}
    coordinator = _coord_with_devices([], data={"daily_yield": point})
    coordinator.daily_yield_diagnostic = None
    sensor = SungrowSensor(coordinator, "daily_yield", "123", "Plant", point)
    assert sensor.extra_state_attributes == {"source": "cloud"}


def test_temperature_unit_glyph_normalized():
    """The API's ℃ glyph (U+2103) is normalised to HA-valid °C for the temperature class.

    HA rejects the single-glyph ℃ as an invalid unit for the temperature device class
    ("expected one of ['K', '°F', '°C']"), which logged a warning on every inverter.
    """
    point = {"id": "4", "code": "internal_temperature", "value": "57.1", "unit": "℃"}
    coordinator = _coord_with_devices([], data={"internal_temperature": point})
    sensor = SungrowSensor(coordinator, "internal_temperature", "123", "Plant", point)
    assert sensor._attr_native_unit_of_measurement == "°C"
    assert sensor._attr_device_class == SensorDeviceClass.TEMPERATURE
    assert sensor.native_value == 57.1


def test_sensor_rehomes_to_singular_device():
    """A mapped point re-homes onto the single device of its type; unique_id unchanged."""
    inv = {
        "uuid": "inv-1",
        "device_type": DeviceType.INVERTER,
        "device_model_code": "SG3.6RS",
        "device_sn": "A1",
        "factory_name": "SUNGROW",
    }
    coordinator = _coord_with_devices([inv])
    sensor = SungrowSensor(
        coordinator, "inverter_ac_power", "123", "Plant", {"code": "inverter_ac_power", "value": "1", "unit": "W"}
    )
    assert (DOMAIN, "inv-1") in sensor._attr_device_info["identifiers"]
    assert sensor._attr_device_info["model"] == "SG3.6RS"
    # Non-breaking: unique_id is still the plant-scoped code.
    assert sensor._attr_unique_id == "123_inverter_ac_power"


def test_sensor_stays_on_plant_when_no_device_or_unmapped():
    """No matching device (or an unmapped code) keeps the sensor on the plant device."""
    # No devices at all -> plant.
    coordinator = _coord_with_devices([])
    sensor = SungrowSensor(
        coordinator, "total_active_power", "123", "Plant", {"code": "total_active_power", "value": "1", "unit": "kW"}
    )
    assert sensor._attr_device_info["identifiers"] == {(DOMAIN, "123")}
    assert sensor._attr_device_info.get("entry_type") is not None
    # Unmapped code with a device present -> still plant.
    coord2 = _coord_with_devices([{"uuid": "inv-1", "device_type": DeviceType.INVERTER}])
    load = SungrowSensor(coord2, "load_power", "123", "Plant", {"code": "load_power", "value": "1", "unit": "W"})
    assert load._attr_device_info["identifiers"] == {(DOMAIN, "123")}


# ---------------------------------------------------------------------------
# Plant-detail sensors (#178)
# ---------------------------------------------------------------------------


def test_plant_detail_sensor_values_and_units():
    """Plant-detail sensors read from coordinator.plant_detail, on the plant device."""
    coordinator = MagicMock()
    coordinator.plant_detail = {
        "alarm_count": 2,
        "install_power": 3600.0,
        "power_price_unit": "GBP",
        "ps_consumption_power_price_kwh": "0.3887",
    }
    descs = {d.key: d for d in PLANT_DETAIL_SENSORS}

    alarm = SungrowPlantDetailSensor(coordinator, descs["alarm_count"], "123", "Plant", "http://x")
    # Counts are whole numbers, displayed without a fractional part.
    assert alarm.native_value == 2
    assert isinstance(alarm.native_value, int)
    assert alarm._attr_suggested_display_precision == 0
    assert alarm._attr_unique_id == "123_detail_alarm_count"
    assert alarm._attr_device_info["identifiers"] == {(DOMAIN, "123")}
    assert alarm._attr_icon == "mdi:alert-outline"

    power = SungrowPlantDetailSensor(coordinator, descs["install_power"], "123", "Plant", "http://x")
    assert power.native_value == 3600.0
    assert power._attr_native_unit_of_measurement == "W"
    assert power._attr_icon == "mdi:solar-power"

    # Tariff sensor takes its unit from the plant's configured currency; import
    # price is money paid (cash-minus).
    price = SungrowPlantDetailSensor(coordinator, descs["ps_consumption_power_price_kwh"], "123", "Plant", "http://x")
    assert price.native_value == 0.3887
    assert price._attr_native_unit_of_measurement == "GBP/kWh"
    assert price._attr_icon == "mdi:cash-minus"
    # Recorded as statistics so time-of-use tariff changes graph over time.
    assert price._attr_state_class == SensorStateClass.MEASUREMENT


def test_plant_detail_sensor_absent_field_is_none():
    """A field missing from plant_detail yields native_value None (builder skips it)."""
    coordinator = MagicMock()
    coordinator.plant_detail = {}
    desc = next(d for d in PLANT_DETAIL_SENSORS if d.key == "fault_count")
    sensor = SungrowPlantDetailSensor(coordinator, desc, "123", "Plant", "http://x")
    assert sensor.native_value is None


def test_fault_count_is_integer():
    """Fault count is a whole number, displayed without decimals."""
    coordinator = MagicMock()
    coordinator.plant_detail = {"fault_count": 4}
    desc = next(d for d in PLANT_DETAIL_SENSORS if d.key == "fault_count")
    sensor = SungrowPlantDetailSensor(coordinator, desc, "123", "Plant", "http://x")
    assert sensor.native_value == 4
    assert isinstance(sensor.native_value, int)
    assert sensor._attr_suggested_display_precision == 0


def test_export_price_icon_is_cash_plus():
    """Export price is money earned (cash-plus); import price is money paid (cash-minus)."""
    coordinator = MagicMock()
    coordinator.plant_detail = {"ps_feedin_power_price_kwh": "0.15", "power_price_unit": "GBP"}
    desc = next(d for d in PLANT_DETAIL_SENSORS if d.key == "ps_feedin_power_price_kwh")
    sensor = SungrowPlantDetailSensor(coordinator, desc, "123", "Plant", "http://x")
    assert sensor._attr_icon == "mdi:cash-plus"


# ---------------------------------------------------------------------------
# Per-device diagnostic naming, icons & classification (aesthetics)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "point_id", "expected_name"),
    [
        ("mppt1_voltage", "5", "MPPT1 Voltage"),
        ("mppt2_current", "8", "MPPT2 Current"),
        ("mppt3_voltage", "9", "MPPT3 Voltage"),
        ("total_dc_power", "14", "Total DC Power"),
        ("negative_voltage_to_ground", "90", "Negative Voltage to Ground"),
        ("afci_fault_count", "120", "AFCI Fault Count"),
        ("wlan_signal_strength", "23014", "WLAN Signal Strength"),
        ("battery_dc_contactor_status", "58635", "Battery DC Contactor Status"),
        ("battery_fault_module_id", "58636", "Battery Fault Module ID"),
    ],
)
def test_acronym_display_names(code, point_id, expected_name):
    """Acronym/initialism codes keep their capitalisation instead of being title-cased."""
    point = {"id": point_id, "code": code, "value": "1", "unit": ""}
    coordinator = _coord_with_devices([], data={code: point})
    sensor = SungrowSensor(coordinator, code, "123", "Plant", point)
    assert sensor._attr_name == expected_name


@pytest.mark.parametrize(
    ("code", "point_id", "unit", "expected_icon"),
    [
        ("array_insulation_resistance", "94", "kΩ", "mdi:omega"),
        ("afci_fault_count", "120", "", "mdi:flash-alert"),
        ("battery_operation_status", "58608", "", "mdi:battery-sync"),
        ("battery_fault_module_id", "58636", "", "mdi:alert-circle"),
    ],
)
def test_diagnostic_icon_overrides(code, point_id, unit, expected_icon):
    """Otherwise-unclassified diagnostics get a fitting icon instead of the solar fallback."""
    point = {"id": point_id, "code": code, "value": "1", "unit": unit}
    coordinator = _coord_with_devices([], data={code: point})
    sensor = SungrowSensor(coordinator, code, "123", "Plant", point)
    assert sensor._attr_icon == expected_icon


def test_afci_fault_count_is_integer_measurement():
    """AFCI fault count graphs as a whole-number measurement, not a text sensor."""
    point = {"id": "120", "code": "afci_fault_count", "value": "3", "unit": ""}
    coordinator = _coord_with_devices([], data={"afci_fault_count": point})
    sensor = SungrowSensor(coordinator, "afci_fault_count", "123", "Plant", point)
    assert sensor._attr_device_class is None
    assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
    assert sensor._attr_suggested_display_precision == 0
    assert sensor.native_value == 3.0


# ---------------------------------------------------------------------------
# SungrowSensor unit tests
# ---------------------------------------------------------------------------


class TestSungrowSensor:
    """Unit tests for SungrowSensor entity."""

    def _make_coordinator(self, data=None):
        coordinator = MagicMock()
        coordinator.data = data or {}
        coordinator.devices = []  # #158: SungrowSensor reads this to pick its device
        coordinator.plants_service = MagicMock()  # cloud-backed coordinator
        coordinator.via_plant_id = None
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
            # SOH is health, not charge level — no BATTERY device class (issue #105).
            ("battery_soh", "%", "Battery Health (SOH)", None),
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

    def test_native_value_none_when_point_value_null(self):
        """A present point whose value is None returns None (not a crash/coercion)."""
        data = {"power": {"code": "power", "value": None, "unit": "W", "name": "Power"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", data["power"])

        assert sensor.native_value is None

    def test_native_value_unclassified_numeric_stays_string(self):
        """A numeric-looking status point without a class is not coerced to a float."""
        data = {"status": {"code": "status", "value": "1", "unit": "", "name": "Status"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "status", "123", "Plant", data["status"])

        # No unit -> no device/state class -> left as a string, not 1.0.
        assert sensor.native_value == "1"
        assert isinstance(sensor.native_value, str)

    def test_native_value_enum_maps_label(self):
        """An enum point (charger status) returns its human label, not the raw code."""
        data = {"ev_charger_status": {"id": "33716", "code": "ev_charger_status", "value": 3, "unit": ""}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "ev_charger_status", "123", "Plant", data["ev_charger_status"])

        assert sensor._attr_device_class == SensorDeviceClass.ENUM
        assert "Charging" in (sensor._attr_options or [])
        assert sensor.native_value == "Charging"

    def test_native_value_enum_unmapped_code_is_none(self):
        """An enum code not in the documented table must not leak outside options (#113)."""
        data = {"ev_charger_status": {"id": "33716", "code": "ev_charger_status", "value": 999, "unit": ""}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "ev_charger_status", "123", "Plant", data["ev_charger_status"])

        assert sensor._attr_device_class == SensorDeviceClass.ENUM
        assert sensor.native_value is None

    def test_native_value_classified_non_numeric_returns_none(self):
        """A classified numeric sensor with a non-numeric value returns None, not raw text (#113)."""
        data = {"power": {"code": "power", "value": "unknown", "unit": "W", "name": "Power"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", data["power"])

        assert sensor._attr_device_class == SensorDeviceClass.POWER
        assert sensor.native_value is None

    def test_native_value_unitless_numeric_is_float(self):
        """A dimensionless power-factor value now coerces to float (was text) (issue #105)."""
        data = {"meter_power_factor": {"id": "8014", "code": "meter_power_factor", "value": "0.98", "unit": ""}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "meter_power_factor", "123", "Plant", data["meter_power_factor"])

        assert sensor._attr_device_class == SensorDeviceClass.POWER_FACTOR
        assert sensor.native_value == 0.98

    def test_capacity_ratio_presented_as_percent(self):
        """A 0–1 capacity-factor ratio (83019) is shown as a percentage, value ×100.

        iSolarCloud reports 'Plant Power / Installed Power' as a bare fraction with no
        unit (e.g. 0.3936). The sensor presents it as '%' scaled to 39.36 so it reads
        as "39.36 %" and graphs, instead of an opaque decimal text sensor.
        """
        data = {"power_fraction": {"id": "83019", "code": "power_fraction", "value": "0.3936", "unit": ""}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power_fraction", "123", "Plant", data["power_fraction"])

        assert sensor._attr_native_unit_of_measurement == "%"
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
        assert sensor._attr_device_class is None
        assert sensor.native_value == pytest.approx(39.36)

    def test_capacity_ratio_zero_stays_zero_percent(self):
        """A zero ratio (night) reads as 0 %, not None or text."""
        data = {"power_fraction": {"id": "83019", "code": "power_fraction", "value": "0", "unit": ""}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power_fraction", "123", "Plant", data["power_fraction"])

        assert sensor.native_value == 0.0

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
    coordinator.plant_detail = {}  # #178: _build_sensors reads this
    coordinator.devices = []
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


async def test_sensor_setup_skips_points_with_no_usable_reading(hass: HomeAssistant):
    """Points that render as Unknown (null / blank / "--" placeholder) are not created.

    The cloud returns the full measure-point catalogue regardless of installed
    hardware, so a PV-only plant gets battery/EMS points back as null or a "--"
    placeholder. Those would render permanently "Unknown", so they are skipped.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with(
        "12345",
        "Plant A",
        {
            "total_active_power": {"value": "5.0", "unit": "kW", "name": "Total Active Power"},  # real -> kept
            "daily_yield": {"value": "0", "unit": "kWh", "name": "Daily Yield"},  # a real zero -> kept
            "battery_power": {"value": "--", "unit": "W", "name": "Battery Power"},  # placeholder -> skipped
            "battery_level_soc": {"value": None, "unit": "%", "name": "SOC"},  # null -> skipped
            "ems_battery_power": {"value": "unknown", "unit": "kW", "name": "EMS"},  # unknown -> skipped
        },
    )
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert {e.point_code for e in added} == {"total_active_power", "daily_yield"}


# ---------------------------------------------------------------------------
# Per-device sensors (issue #74)
# ---------------------------------------------------------------------------


async def test_device_sensors_created_when_enabled(hass: HomeAssistant):
    """Device points not already at plant level become sensors under their device."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    coordinator.enable_device_sensors = True
    coordinator.via_plant_id = None
    coordinator.local_configuration_url = None
    # The platform reads the live device list from the coordinator for naming.
    coordinator.devices = [
        {
            "uuid": "chg-1",
            "device_name": "AC011E",
            "device_type": 999,
            "device_model_code": "AC011E-01",
            "device_sn": "S1234567",
            "factory_name": "SUNGROW",
        }
    ]
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
    # Device card is enriched with the cloud's model/serial/manufacturer (#149).
    assert sensor._attr_device_info["model"] == "AC011E-01"
    assert sensor._attr_device_info["serial_number"] == "S1234567"
    assert sensor._attr_device_info["manufacturer"] == "SUNGROW"
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


async def test_sensor_dynamic_add_when_placeholder_becomes_real(hass: HomeAssistant):
    """A point skipped as Unknown at setup is added once it starts reporting real data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with(
        "12345",
        "Plant A",
        {
            "total_active_power": {"value": "5.0", "unit": "kW"},
            "battery_power": {"value": "--", "unit": "W", "name": "Battery Power"},  # skipped at setup
        },
    )
    listeners: list = []
    coordinator.async_add_listener = lambda cb, *a: listeners.append(cb) or (lambda: None)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert {e.point_code for e in added} == {"total_active_power"}

    # The battery starts reporting a real value on a later poll.
    coordinator.data = {**coordinator.data, "battery_power": {"value": "1500", "unit": "W", "name": "Battery Power"}}
    for cb in listeners:
        cb()
    assert {e.point_code for e in added} == {"total_active_power", "battery_power"}


async def test_inverter_diagnostic_sensor_is_diagnostic_and_enum(hass: HomeAssistant):
    """Operating-status device sensor is DIAGNOSTIC and maps its code to a label (#149)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    coordinator.enable_device_sensors = True
    coordinator.devices = [{"uuid": "inv-1", "device_name": "Inverter1", "device_type": DeviceType.INVERTER}]
    coordinator.device_data = {"inv-1": {"operating_status": {"id": "29", "code": "operating_status", "value": "64"}}}
    entry.runtime_data = SungrowData(
        coordinators=[coordinator], control=MagicMock(), devices={"12345": coordinator.devices}
    )

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    sensor = next(e for e in added if isinstance(e, SungrowDeviceSensor) and e.point_code == "operating_status")
    assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert sensor._attr_device_class == SensorDeviceClass.ENUM
    # Raw code "64" maps to a human label from the operating-status enum.
    assert sensor.native_value not in (None, "64")


async def test_battery_device_sensor_categories(hass: HomeAssistant):
    """Battery health points are diagnostic; SOC stays a primary sensor (#154)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    coordinator.enable_device_sensors = True
    coordinator.devices = [{"uuid": "bat-1", "device_name": "Battery1", "device_type": DeviceType.BATTERY}]
    coordinator.device_data = {
        "bat-1": {
            "battery_temperature": {"id": "58603", "code": "battery_temperature", "value": "25.0", "unit": "°C"},
            "battery_level": {"id": "58604", "code": "battery_level", "value": "80", "unit": "%"},
        }
    }
    entry.runtime_data = SungrowData(
        coordinators=[coordinator], control=MagicMock(), devices={"12345": coordinator.devices}
    )

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    by_code = {e.point_code: e for e in added if isinstance(e, SungrowDeviceSensor)}
    assert by_code["battery_temperature"].entity_category == EntityCategory.DIAGNOSTIC
    assert by_code["battery_temperature"].device_class == SensorDeviceClass.TEMPERATURE
    assert by_code["battery_level"].entity_category is None  # SOC is a primary sensor
    assert by_code["battery_level"].device_class == SensorDeviceClass.BATTERY


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


# ---------------------------------------------------------------------------
# Bug condition exploration (hybrid MPPT / string sensors) — Property 1
#
# The hybrid MPPT codes reuse the string-inverter mpptN_* code names. mppt1-3 are
# already in _DIAGNOSTIC_CODES via INVERTER_DIAGNOSTIC_POINTS ("5"-"10"), but mppt4_*
# are NEW code names not present in that map, so on the UNFIXED code they are NOT in
# _DIAGNOSTIC_CODES and this test FAILS — confirming the classification gap.
# Validates: Requirements 2.2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["mppt4_voltage", "mppt4_current"])
def test_hybrid_mppt4_codes_are_diagnostic(code):
    """Hybrid MPPT4 voltage/current codes must be classified diagnostic (Property 1).

    On the UNFIXED code _DIAGNOSTIC_CODES is derived only from INVERTER_DIAGNOSTIC_POINTS
    (mppt1-3) plus battery/comm codes, so mppt4_voltage/mppt4_current are absent and this
    assertion FAILS — which is the intended proof that hybrid MPPT4 sensors would land in
    the main sensors instead of the Diagnostic section.

    Validates: Requirements 2.2
    """
    from custom_components.sungrow.sensor import _DIAGNOSTIC_CODES

    assert code in _DIAGNOSTIC_CODES, f"{code} is not classified as diagnostic (not in _DIAGNOSTIC_CODES)"


# ---------------------------------------------------------------------------
# Preservation (hybrid MPPT / string sensors) — Property 2
#
# The upcoming fix unions ESS_MPPT_DIAGNOSTIC_POINTS.values() into _DIAGNOSTIC_CODES.
# The union is additive, so every existing string-inverter diagnostic code must remain
# classified as diagnostic afterwards. This baseline check passes on the unfixed code.
# Validates: Requirement 3.5
# ---------------------------------------------------------------------------


def test_inverter_diagnostic_codes_stay_diagnostic():
    """Every existing INVERTER_DIAGNOSTIC_POINTS code remains a member of _DIAGNOSTIC_CODES.

    Guards the diagnostic classification of string-inverter MPPT/string/grid-health codes
    against regression when the hybrid MPPT codes are later unioned in.

    Validates: Requirement 3.5
    """
    from custom_components.sungrow.const import INVERTER_DIAGNOSTIC_POINTS
    from custom_components.sungrow.sensor import _DIAGNOSTIC_CODES

    missing = set(INVERTER_DIAGNOSTIC_POINTS.values()) - _DIAGNOSTIC_CODES
    assert not missing, f"string-inverter diagnostic codes dropped from _DIAGNOSTIC_CODES: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Fix Checking (hybrid MPPT / string sensors) — Property 1, Requirement 2.3
#
# Integration-style: an ESS device whose realtime response reports only MPPT1/MPPT2
# produces exactly those diagnostic sensors; MPPT3/MPPT4 are silently skipped (no
# sensor created), consistent with the string-inverter builder behavior.
# ---------------------------------------------------------------------------


async def test_ess_partial_mppt_only_reported_sensors_created(hass: HomeAssistant):
    """Property 1 (Req 2.3): only the MPPTs the ESS actually reports become sensors.

    The per-device builder skips points a model does not report, so a hybrid with only
    MPPT1/MPPT2 wired produces mppt1/mppt2 voltage+current sensors and no mppt3/mppt4.
    Mirrors test_battery_device_sensor_categories.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    coordinator = _coordinator_with("12345", "Plant A", {"total_active_power": {"value": "5.0", "unit": "kW"}})
    coordinator.enable_device_sensors = True
    coordinator.devices = [{"uuid": "ess-1", "device_name": "Hybrid1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM}]
    # The cloud returns only MPPT1/MPPT2 for this hybrid; MPPT3/MPPT4 are unreported.
    coordinator.device_data = {
        "ess-1": {
            "mppt1_voltage": {"id": "13001", "code": "mppt1_voltage", "value": "410.5", "unit": "V"},
            "mppt1_current": {"id": "13002", "code": "mppt1_current", "value": "8.1", "unit": "A"},
            "mppt2_voltage": {"id": "13105", "code": "mppt2_voltage", "value": "395.2", "unit": "V"},
            "mppt2_current": {"id": "13106", "code": "mppt2_current", "value": "7.4", "unit": "A"},
        }
    }
    entry.runtime_data = SungrowData(
        coordinators=[coordinator], control=MagicMock(), devices={"12345": coordinator.devices}
    )

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    by_code = {e.point_code: e for e in added if isinstance(e, SungrowDeviceSensor)}
    # Only the reported MPPT1/MPPT2 sensors exist.
    assert set(by_code) == {"mppt1_voltage", "mppt1_current", "mppt2_voltage", "mppt2_current"}
    # MPPT3/MPPT4 are silently skipped (never reported -> no sensor).
    for code in ("mppt3_voltage", "mppt3_current", "mppt4_voltage", "mppt4_current"):
        assert code not in by_code
    # The reported MPPT sensors land in the Diagnostic section with the right classes.
    assert by_code["mppt1_voltage"].entity_category == EntityCategory.DIAGNOSTIC
    assert by_code["mppt1_voltage"].device_class == SensorDeviceClass.VOLTAGE
    assert by_code["mppt2_current"].entity_category == EntityCategory.DIAGNOSTIC
    assert by_code["mppt2_current"].device_class == SensorDeviceClass.CURRENT
