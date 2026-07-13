"""Unit tests for options flow transport switching (#216)."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import data_entry_flow
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
# cloud_only entry: shows optional modbus_host, can switch to hybrid
# ---------------------------------------------------------------------------


async def test_options_cloud_only_shows_modbus_host_field(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A cloud_only entry's options include an optional modbus_host field."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(m.schema) for m in result["data_schema"].schema}
    assert CONF_MODBUS_HOST in keys


async def test_options_cloud_only_switch_to_hybrid(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Providing a reachable host switches cloud_only → cloud_modbus."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=True):
        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_SCAN_INTERVAL: 300, CONF_MODBUS_HOST: "192.168.1.50"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_MODBUS
    assert entry.data[CONF_MODBUS_HOST] == "192.168.1.50"


async def test_options_cloud_only_unreachable_host_shows_error(
    hass: HomeAssistant, mock_setup_auth, mock_plants_service
):
    """Unreachable host keeps cloud_only and shows error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=False):
        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_SCAN_INTERVAL: 300, CONF_MODBUS_HOST: "10.0.0.99"},
        )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"]["base"] == "host_unreachable"
    # Transport unchanged
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY


# ---------------------------------------------------------------------------
# cloud_modbus entry: shows host with clear option
# ---------------------------------------------------------------------------


async def test_options_cloud_modbus_shows_host(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A cloud_modbus entry's options show the current modbus_host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_MODBUS, CONF_MODBUS_HOST: "192.168.1.50"},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(m.schema) for m in result["data_schema"].schema}
    assert CONF_MODBUS_HOST in keys


async def test_options_cloud_modbus_clear_host_switches_to_cloud_only(
    hass: HomeAssistant, mock_setup_auth, mock_plants_service
):
    """Clearing the host on a cloud_modbus entry switches back to cloud_only."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_MODBUS, CONF_MODBUS_HOST: "192.168.1.50"},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 300, CONF_MODBUS_HOST: ""},
    )
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    assert CONF_MODBUS_HOST not in entry.data


# ---------------------------------------------------------------------------
# modbus_only entry: no transport switching
# ---------------------------------------------------------------------------


async def test_options_modbus_only_no_switch(hass: HomeAssistant):
    """A modbus_only entry does not show the transport-switch field."""
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

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "modbus_options"
        keys = {str(m.schema) for m in result["data_schema"].schema}
        assert CONF_MODBUS_HOST not in keys
