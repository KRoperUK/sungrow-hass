"""Smoke tests for transport-mode constants (#216)."""

from custom_components.sungrow.config_flow import SungrowConfigFlow
from custom_components.sungrow.const import (
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)


def test_transport_cloud_only_value():
    """TRANSPORT_CLOUD_ONLY equals 'cloud_only'."""
    assert TRANSPORT_CLOUD_ONLY == "cloud_only"


def test_transport_cloud_modbus_value():
    """TRANSPORT_CLOUD_MODBUS equals 'cloud_modbus'."""
    assert TRANSPORT_CLOUD_MODBUS == "cloud_modbus"


def test_transport_modbus_only_value():
    """TRANSPORT_MODBUS_ONLY equals 'modbus_only'."""
    assert TRANSPORT_MODBUS_ONLY == "modbus_only"


def test_config_flow_version_is_current():
    """SungrowConfigFlow.VERSION reflects the latest schema (v3→v4 legacy sweep, #314)."""
    assert SungrowConfigFlow.VERSION == 4
