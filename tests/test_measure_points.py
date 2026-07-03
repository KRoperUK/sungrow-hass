"""Tests for the Sungrow measuring-point catalog and resolvers."""

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.sungrow import measure_points as mp

M = SensorStateClass.MEASUREMENT
TI = SensorStateClass.TOTAL_INCREASING


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("W", (SensorDeviceClass.POWER, M)),
        ("kWh", (SensorDeviceClass.ENERGY, TI)),
        ("V", (SensorDeviceClass.VOLTAGE, M)),
        ("mV", (SensorDeviceClass.VOLTAGE, M)),
        ("A", (SensorDeviceClass.CURRENT, M)),
        ("Hz", (SensorDeviceClass.FREQUENCY, M)),
        ("°C", (SensorDeviceClass.TEMPERATURE, M)),
        ("var", (SensorDeviceClass.REACTIVE_POWER, M)),
        ("VA", (SensorDeviceClass.APPARENT_POWER, M)),
        # New units for the broader catalogs.
        ("%RH", (SensorDeviceClass.HUMIDITY, M)),
        ("m/s", (SensorDeviceClass.WIND_SPEED, M)),
        ("hPa", (SensorDeviceClass.PRESSURE, M)),
        ("mm", (SensorDeviceClass.PRECIPITATION, M)),
        ("W/m²", (SensorDeviceClass.IRRADIANCE, M)),
        ("h", (SensorDeviceClass.DURATION, M)),
        ("varh", (None, TI)),
        ("kΩ", (None, M)),
        # Case/space-insensitive.
        ("kwh", (SensorDeviceClass.ENERGY, TI)),
        (" W ", (SensorDeviceClass.POWER, M)),
    ],
)
def test_classify_by_unit_known(unit, expected):
    assert mp._classify_by_unit(unit) == expected


@pytest.mark.parametrize("unit", ["", None, "widgets", "%"])
def test_classify_by_unit_unknown_returns_none(unit):
    assert mp._classify_by_unit(unit) is None


# ---------------------------------------------------------------------------
# Enum resolvers
# ---------------------------------------------------------------------------


def test_enum_options_charger_status():
    opts = mp.resolve_enum_options("33716")
    assert opts is not None
    assert "Charging" in opts
    assert "Idle (not plugged in)" in opts
    # Distinct + order-preserving.
    assert len(opts) == len(set(opts))


def test_enum_options_none_for_non_enum():
    assert mp.resolve_enum_options("8018") is None


def test_enum_value_maps_int():
    assert mp.resolve_enum_value("33716", 3) == "Charging"
    assert mp.resolve_enum_value("33716", "3") == "Charging"
    assert mp.resolve_enum_value("33716", 3.0) == "Charging"


def test_enum_value_unmapped_code_falls_back_to_str():
    assert mp.resolve_enum_value("33716", 999) == "999"


def test_enum_value_none_for_non_enum():
    assert mp.resolve_enum_value("8018", 5) is None


# ---------------------------------------------------------------------------
# resolve_classification
# ---------------------------------------------------------------------------


def test_classify_enum_point():
    assert mp.resolve_classification("", "ev_charger_status", "33716") == (
        SensorDeviceClass.ENUM,
        None,
    )


def test_classify_unit_wins():
    assert mp.resolve_classification("kWh", "anything", "0") == (
        SensorDeviceClass.ENERGY,
        TI,
    )


def test_classify_percent_battery():
    assert mp.resolve_classification("%", "battery_soc", "0") == (SensorDeviceClass.BATTERY, M)


def test_classify_percent_soh_is_not_battery():
    # SOH is health, not charge level — must NOT be BATTERY device class.
    assert mp.resolve_classification("%", "battery_soh", "0") == (None, M)


def test_classify_percent_generic():
    assert mp.resolve_classification("%", "efficiency", "0") == (None, M)


def test_classify_dimensionless_power_factor_by_code():
    assert mp.resolve_classification("", "meter_power_factor", "0") == (
        SensorDeviceClass.POWER_FACTOR,
        M,
    )


def test_classify_dimensionless_soc_by_code():
    assert mp.resolve_classification(None, "total_field_soc", "0") == (SensorDeviceClass.BATTERY, M)


def test_classify_unknown_is_text():
    assert mp.resolve_classification("", "some_status", "0") == (None, None)
