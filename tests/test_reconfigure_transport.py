"""Unit tests for reconfigure flow adaptation per transport mode (#216)."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_REDIRECT_URI,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

from .conftest import MOCK_CONFIG_DATA


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession."""
    with patch(
        "custom_components.sungrow.config_flow.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# cloud_only reconfigure: shows credentials form
# ---------------------------------------------------------------------------


async def test_reconfigure_cloud_only_shows_credentials(hass: HomeAssistant, mock_auth):
    """A cloud_only entry reconfigure shows the credentials form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


# ---------------------------------------------------------------------------
# cloud_modbus reconfigure: credentials + modbus host step
# ---------------------------------------------------------------------------


async def test_reconfigure_cloud_modbus_shows_host_step(hass: HomeAssistant, mock_auth):
    """A cloud_modbus entry reconfigure shows credentials then modbus_host step."""
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"

    # Submit credentials
    new_input = {
        CONF_APP_KEY: "new_key",
        CONF_APP_SECRET: "new_secret",
        CONF_GATEWAY: "Europe",
        CONF_REDIRECT_URI: MOCK_CONFIG_DATA[CONF_REDIRECT_URI],
    }
    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=new_input)
    assert result2["step_id"] == "reconfigure_modbus_host"


async def test_reconfigure_cloud_modbus_host_failure(hass: HomeAssistant, mock_auth):
    """Reconfigure cloud_modbus with unreachable host shows error."""
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    new_input = {
        CONF_APP_KEY: "k",
        CONF_APP_SECRET: "s",
        CONF_GATEWAY: "Europe",
        CONF_REDIRECT_URI: MOCK_CONFIG_DATA[CONF_REDIRECT_URI],
    }
    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=new_input)
    assert result2["step_id"] == "reconfigure_modbus_host"

    with patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=False):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "10.0.0.99"}
        )

    assert result3["type"] == data_entry_flow.FlowResultType.FORM
    assert result3["errors"]["base"] == "host_unreachable"


# ---------------------------------------------------------------------------
# modbus_only reconfigure: shows host form only
# ---------------------------------------------------------------------------


async def test_reconfigure_modbus_only_shows_host_form(hass: HomeAssistant):
    """A modbus_only entry reconfigure shows the modbus host form only."""
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

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure_modbus"
    keys = {str(m.schema) for m in result["data_schema"].schema}
    assert keys == {CONF_MODBUS_HOST}
