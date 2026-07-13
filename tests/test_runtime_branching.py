"""Integration tests for runtime branching on transport mode (#216)."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import (
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

from .conftest import MOCK_CONFIG_DATA

# ---------------------------------------------------------------------------
# cloud_only → standard cloud coordinator
# ---------------------------------------------------------------------------


async def test_setup_entry_cloud_only(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """cloud_only transport sets up the cloud coordinator normally."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state.name == "LOADED"
    # Cloud coordinator creates plant data
    assert entry.runtime_data.coordinators


# ---------------------------------------------------------------------------
# cloud_modbus → cloud coordinator + info log
# ---------------------------------------------------------------------------


async def test_setup_entry_cloud_modbus_logs_deferred(
    hass: HomeAssistant, mock_setup_auth, mock_plants_service, caplog
):
    """cloud_modbus transport sets up cloud coordinator and logs deferred message."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **MOCK_CONFIG_DATA,
            CONF_TRANSPORT: TRANSPORT_CLOUD_MODBUS,
            CONF_MODBUS_HOST: "192.168.1.50",
        },
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)

    with caplog.at_level(logging.INFO, logger="custom_components.sungrow"):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.name == "LOADED"
    assert any("deferred to #217" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# modbus_only → calls _async_setup_modbus_only
# ---------------------------------------------------------------------------


async def test_setup_entry_modbus_only(hass: HomeAssistant):
    """modbus_only transport calls the Modbus-only setup path."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "SN123",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "10.0.0.9",
        },
        options={CONF_SCAN_INTERVAL: 30},
        unique_id="modbus_SN123",
    )
    entry.add_to_hass(hass)

    client = MagicMock()
    client.async_read_realtime = AsyncMock(return_value={"grid_frequency": {"value": 49.9}})
    with patch("custom_components.sungrow.modbus.SungrowModbusClient", return_value=client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.name == "LOADED"


# ---------------------------------------------------------------------------
# missing transport → defaults to cloud_only + warning
# ---------------------------------------------------------------------------


async def test_setup_entry_missing_transport_defaults_cloud_only(
    hass: HomeAssistant, mock_setup_auth, mock_plants_service, caplog
):
    """Missing transport field defaults to cloud_only and logs a warning."""
    # Remove CONF_TRANSPORT from data entirely
    data = {k: v for k, v in MOCK_CONFIG_DATA.items() if k != CONF_TRANSPORT}
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id="test_app_id", version=3)
    entry.add_to_hass(hass)

    with caplog.at_level(logging.WARNING, logger="custom_components.sungrow"):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.name == "LOADED"
    assert any("missing CONF_TRANSPORT" in msg for msg in caplog.messages)
