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


def test_transport_cloud_modbus_value_kept_for_migration():
    """The ``cloud_modbus`` constant is retained after #348 retired the transport.

    The v4→v5 migration converts legacy ``cloud_modbus`` entries to ``cloud_only``,
    so it still needs to detect the string. The constant is not exposed in the
    config-flow selector any more (see ``test_transport_flow.py``).
    """
    assert TRANSPORT_CLOUD_MODBUS == "cloud_modbus"


def test_transport_modbus_only_value():
    """TRANSPORT_MODBUS_ONLY equals 'modbus_only'."""
    assert TRANSPORT_MODBUS_ONLY == "modbus_only"


def test_config_flow_version_is_current():
    """SungrowConfigFlow.VERSION reflects the latest schema.

    v4→v5 retires cloud_modbus (#348); v5→v6 restores the canonical local-Modbus
    yield codes (#382).
    """
    assert SungrowConfigFlow.VERSION == 6
