"""Unit tests for options flow transport handling.

Originally covered the cloud_only ↔ cloud_modbus transport switching in the
options flow (#216). The ``cloud_modbus`` transport was retired in #348 —
its options-flow branches, the modbus_host field on cloud entries, and the
switch-in-both-directions logic are all gone. The two tests kept below cover:

* a cloud_only options flow no longer offers ``modbus_host`` (regression
  guard against re-introducing the retired transport by accident);
* a modbus_only entry's options are unchanged (its own separate flow).
"""

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
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

from .conftest import MOCK_CONFIG_DATA


async def test_options_cloud_only_has_no_modbus_host_field(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A cloud_only entry's options no longer expose the modbus_host field (#348)."""
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
    assert CONF_MODBUS_HOST not in keys


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
