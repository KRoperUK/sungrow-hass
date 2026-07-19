"""Unit tests for the transport-mode selector config flow (#216)."""

from ipaddress import ip_address
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.sungrow.const import (
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

from .conftest import MOCK_USER_INPUT


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession."""
    with patch(
        "custom_components.sungrow.config_flow.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# Transport selector step
# ---------------------------------------------------------------------------


async def test_step_user_shows_transport_selector(hass: HomeAssistant):
    """async_step_user shows the transport selector form with 3 options."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    # The schema should have a transport field
    keys = {str(m.schema) for m in result["data_schema"].schema}
    assert CONF_TRANSPORT in keys


# ---------------------------------------------------------------------------
# Cloud Only flow
# ---------------------------------------------------------------------------


async def test_cloud_only_flow_creates_correct_entry(hass: HomeAssistant):
    """user → cloud_credentials → creates entry with transport=cloud_only."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY}
    )
    assert result2["step_id"] == "cloud_credentials"

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result3 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)
        await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result3["data"][CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    assert CONF_MODBUS_HOST not in result3["data"]


# ---------------------------------------------------------------------------
# Cloud + Modbus flow
# ---------------------------------------------------------------------------


async def test_cloud_modbus_transport_no_longer_offered(hass: HomeAssistant):
    """``cloud_modbus`` was retired in #348 — it must not appear in the transport selector."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    transport_selector = result["data_schema"].schema[CONF_TRANSPORT]
    # ``SelectSelector`` exposes its options via ``config["options"]``.
    option_values = {opt["value"] for opt in transport_selector.config["options"]}
    assert TRANSPORT_CLOUD_MODBUS not in option_values


# ---------------------------------------------------------------------------
# Modbus Only flow
# ---------------------------------------------------------------------------


async def test_modbus_only_flow_creates_correct_entry(hass: HomeAssistant):
    """user → local_setup → creates entry with transport=modbus_only + host/serial/model."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY}
    )
    assert result2["step_id"] == "local_setup"

    with (
        patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=True),
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_MODBUS_HOST: "10.0.0.5", CONF_SERIAL: "SN123", CONF_MODEL: "SG3.6RS"},
        )
        await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result3["data"][CONF_TRANSPORT] == TRANSPORT_MODBUS_ONLY
    assert result3["data"][CONF_MODBUS_HOST] == "10.0.0.5"
    assert result3["data"][CONF_SERIAL] == "SN123"
    assert result3["data"][CONF_MODEL] == "SG3.6RS"


# ---------------------------------------------------------------------------
# Zeroconf bypasses transport step
# ---------------------------------------------------------------------------


async def test_zeroconf_bypasses_transport_step(hass: HomeAssistant):
    """Zeroconf discovery flow does NOT show the transport step."""
    discovery = ZeroconfServiceInfo(
        ip_address=ip_address("192.168.1.93"),
        ip_addresses=[ip_address("192.168.1.93")],
        port=80,
        hostname="SUNGROW.local.",
        type="_http._tcp.local.",
        name="WiNet-WebServer._http._tcp.local.",
        properties={"inverter": "1;9732;A2340512345;1;516;SG3.6RS;1;1;"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=discovery
    )
    # Goes directly to zeroconf_confirm, not the transport selector.
    assert result["step_id"] == "zeroconf_confirm"


# ---------------------------------------------------------------------------
# Modbus host reachability errors
# ---------------------------------------------------------------------------


async def test_local_setup_unreachable_shows_error(hass: HomeAssistant):
    """Submitting an unreachable host in local_setup shows an error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY}
    )

    with patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=False):
        result_local = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_MODBUS_HOST: "10.0.0.99", CONF_SERIAL: "SN1", CONF_MODEL: "SG3.6RS"},
        )

    assert result_local["type"] == data_entry_flow.FlowResultType.FORM
    assert result_local["errors"]["base"] == "host_unreachable"
