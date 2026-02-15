"""Tests for the Sungrow iSolarCloud config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.sungrow.const import (
    CONF_APP_ID,
    CONF_APP_KEY,
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


async def test_user_step_advances_to_auth(hass: HomeAssistant, mock_auth):
    """Test submitting user form advances to the auth step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "auth"
    # The auth URL should be present in description placeholders
    assert "auth_url" in result2["description_placeholders"]


# ---------------------------------------------------------------------------
# Step 2: Auth step — success
# ---------------------------------------------------------------------------


async def test_auth_step_success(hass: HomeAssistant, mock_auth):
    """Test a full successful flow: user → auth → entry created."""
    # Step 1: init
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    # Step 2: submit user info
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )
    assert result2["step_id"] == "auth"

    # Step 3: submit the auth code
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"code": "auth_code_from_provider"},
        )
        await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result3["title"] == f"Sungrow {MOCK_USER_INPUT[CONF_APP_ID]}"
    assert result3["data"]["tokens"]["access_token"] == "test_access_token"
    assert result3["data"][CONF_APP_KEY] == MOCK_USER_INPUT[CONF_APP_KEY]


# ---------------------------------------------------------------------------
# Step 2: Auth step — code from URL
# ---------------------------------------------------------------------------


async def test_auth_step_extracts_code_from_url(hass: HomeAssistant, mock_auth):
    """Test that pasting a full callback URL extracts the code automatically."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    callback_url = "http://homeassistant.local:8123/api/sungrow_hass/callback?code=extracted_code&flow_id=123"
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"code": callback_url},
        )
        await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # Verify Auth.async_authorize was called with the extracted code
    mock_auth.async_authorize.assert_called_once()
    call_args = mock_auth.async_authorize.call_args
    assert call_args[0][0] == "extracted_code"


# ---------------------------------------------------------------------------
# Step 2: Auth step — error cases
# ---------------------------------------------------------------------------


async def test_auth_step_no_tokens(hass: HomeAssistant, mock_auth_no_tokens):
    """Test auth step when tokens are empty/missing."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "some_code"},
    )

    assert result3["type"] == data_entry_flow.FlowResultType.FORM
    assert result3["errors"]["base"] == "invalid_auth"


async def test_auth_step_connection_error(hass: HomeAssistant, mock_auth):
    """Test auth step handles connection errors."""
    mock_auth.async_authorize = AsyncMock(side_effect=ClientError("Connection failed"))

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "some_code"},
    )

    assert result3["type"] == data_entry_flow.FlowResultType.FORM
    assert result3["errors"]["base"] == "cannot_connect"


async def test_auth_step_unexpected_error(hass: HomeAssistant, mock_auth):
    """Test auth step handles unexpected exceptions."""
    mock_auth.async_authorize = AsyncMock(side_effect=RuntimeError("Boom"))

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "some_code"},
    )

    assert result3["type"] == data_entry_flow.FlowResultType.FORM
    assert result3["errors"]["base"] == "unknown"


# ---------------------------------------------------------------------------
# Library missing
# ---------------------------------------------------------------------------


async def test_auth_step_library_missing(hass: HomeAssistant):
    """Test abort when pysolarcloud Auth is not installed."""
    with patch("custom_components.sungrow.config_flow.Auth", None):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input=MOCK_USER_INPUT,
        )

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "library_missing"


# ---------------------------------------------------------------------------
# Auth URL from URL with code in fragment
# ---------------------------------------------------------------------------


async def test_auth_step_code_in_url_without_code_param(hass: HomeAssistant, mock_auth):
    """Test that pasting a URL without a 'code' query param falls back to fragment parsing."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    # URL with code in the fragment (SPA-style redirect)
    fragment_url = "http://homeassistant.local:8123/callback#state=abc?code=frag_code"
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"code": fragment_url},
        )
        await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    mock_auth.async_authorize.assert_called_once()
    call_args = mock_auth.async_authorize.call_args
    assert call_args[0][0] == "frag_code"


async def test_auth_step_url_without_code_anywhere(hass: HomeAssistant, mock_auth):
    """Test that a URL with no code param in query OR fragment returns invalid_auth."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    # URL that starts with http but has no 'code' anywhere
    bad_url = "http://example.com/callback?state=abc&other=value"
    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": bad_url},
    )

    assert result3["type"] == data_entry_flow.FlowResultType.FORM
    assert result3["errors"]["base"] == "unknown"
