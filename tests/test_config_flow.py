"""Tests for the Sungrow iSolarCloud config flow (API v1)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.sungrow.const import (
    CONF_APP_KEY,
    CONF_USERNAME,
    DOMAIN,
)

from .conftest import MOCK_USER_INPUT


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession to prevent background thread creation."""
    with patch(
        "custom_components.sungrow.config_flow.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# Step 1: User form
# ---------------------------------------------------------------------------


async def test_user_step_shows_form(hass: HomeAssistant):
    """Test the initial user step shows a form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert "description_placeholders" in result
    assert result["description_placeholders"]["url"] == "https://developer-api.isolarcloud.com/#/application"


# ---------------------------------------------------------------------------
# Successful login
# ---------------------------------------------------------------------------


async def test_user_step_success(hass: HomeAssistant, mock_api):
    """Test a full successful flow: user input -> login -> entry created."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["title"] == f"Sungrow v1 {MOCK_USER_INPUT[CONF_USERNAME]}"
    assert result2["data"]["token"] == "test_token_123"
    assert result2["data"][CONF_APP_KEY] == MOCK_USER_INPUT[CONF_APP_KEY]


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


async def test_user_step_invalid_auth(hass: HomeAssistant, mock_api_invalid_auth):
    """Test login with invalid credentials shows error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"]["base"] == "invalid_auth"


async def test_user_step_connection_error(hass: HomeAssistant):
    """Test login handles connection errors."""
    with patch("custom_components.sungrow.config_flow.ISolarCloudAPI") as mock_api_cls:
        api_instance = MagicMock()
        api_instance.async_login = AsyncMock(side_effect=ClientError("Connection failed"))
        mock_api_cls.return_value = api_instance

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"]["base"] == "cannot_connect"


async def test_user_step_unexpected_error(hass: HomeAssistant):
    """Test login handles unexpected exceptions."""
    with patch("custom_components.sungrow.config_flow.ISolarCloudAPI") as mock_api_cls:
        api_instance = MagicMock()
        api_instance.async_login = AsyncMock(side_effect=RuntimeError("Boom"))
        mock_api_cls.return_value = api_instance

        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"]["base"] == "unknown"
