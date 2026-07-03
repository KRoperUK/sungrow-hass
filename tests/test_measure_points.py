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
