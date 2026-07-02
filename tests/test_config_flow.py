"""Tests for the Sungrow iSolarCloud config flow."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_USER_INPUT


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
    assert "app_id_url" in result["description_placeholders"]


async def test_user_step_advances_to_auth(hass: HomeAssistant, mock_auth):
    """Test submitting user form advances to the auth menu."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )

    assert result2["type"] == data_entry_flow.FlowResultType.MENU
    assert result2["step_id"] == "auth"
    # The auth URL should be present in description placeholders
    assert "auth_url" in result2["description_placeholders"]


# ---------------------------------------------------------------------------
# Step 2: Auth step — success
# ---------------------------------------------------------------------------


async def test_auth_step_success(hass: HomeAssistant, mock_auth):
    """Test a full successful flow: user -> auth menu -> manual -> entry created."""
    # Step 1: init
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    # Step 2: submit user info -> auth menu
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=MOCK_USER_INPUT,
    )
    assert result2["step_id"] == "auth"

    # Step 3: choose manual entry
    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "auth_manual"},
    )
    assert result3["step_id"] == "auth_manual"

    # Step 4: submit the auth code
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result4 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"code": "auth_code_from_provider"},
        )
        await hass.async_block_till_done()

    assert result4["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result4["title"] == f"Sungrow {MOCK_USER_INPUT[CONF_APP_ID]}"
    assert result4["data"]["tokens"]["access_token"] == "test_access_token"
    assert result4["data"][CONF_APP_KEY] == MOCK_USER_INPUT[CONF_APP_KEY]


# ---------------------------------------------------------------------------
# Step 2: Auth step — code from URL
# ---------------------------------------------------------------------------


async def test_auth_step_extracts_code_from_url(hass: HomeAssistant, mock_auth):
    """Test that pasting a full callback URL extracts the code automatically."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"next_step_id": "auth_manual"})

    callback_url = "http://homeassistant.local:8123/api/sungrow_hass/callback?code=extracted_code&flow_id=123"
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result4 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"code": callback_url},
        )
        await hass.async_block_till_done()

    assert result4["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # Verify Auth.async_authorize was called with the extracted code
    mock_auth.async_authorize.assert_called_once()
    call_args = mock_auth.async_authorize.call_args
    assert call_args[0][0] == "extracted_code"


# ---------------------------------------------------------------------------
# Step 2: Auth step — error cases
# ---------------------------------------------------------------------------


async def _navigate_to_auth_manual(hass: HomeAssistant, flow_id: str):
    """Helper: move from the auth menu to the manual code entry step."""
    await hass.config_entries.flow.async_configure(flow_id, user_input=MOCK_USER_INPUT)
    await hass.config_entries.flow.async_configure(flow_id, user_input={"next_step_id": "auth_manual"})


async def test_auth_step_no_tokens(hass: HomeAssistant, mock_auth_no_tokens):
    """Test auth step when tokens are empty/missing."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await _navigate_to_auth_manual(hass, result["flow_id"])

    result4 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "some_code"},
    )

    assert result4["type"] == data_entry_flow.FlowResultType.FORM
    assert result4["errors"]["base"] == "invalid_auth"


async def test_auth_step_connection_error(hass: HomeAssistant, mock_auth):
    """Test auth step handles connection errors."""
    mock_auth.async_authorize = AsyncMock(side_effect=ClientError("Connection failed"))

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await _navigate_to_auth_manual(hass, result["flow_id"])

    result4 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "some_code"},
    )

    assert result4["type"] == data_entry_flow.FlowResultType.FORM
    assert result4["errors"]["base"] == "cannot_connect"


async def test_auth_step_unexpected_error(hass: HomeAssistant, mock_auth):
    """Test auth step handles unexpected exceptions."""
    mock_auth.async_authorize = AsyncMock(side_effect=RuntimeError("Boom"))

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await _navigate_to_auth_manual(hass, result["flow_id"])

    result4 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "some_code"},
    )

    assert result4["type"] == data_entry_flow.FlowResultType.FORM
    assert result4["errors"]["base"] == "unknown"


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
    await _navigate_to_auth_manual(hass, result["flow_id"])

    # URL with code in the fragment (SPA-style redirect)
    fragment_url = "http://homeassistant.local:8123/callback#state=abc?code=frag_code"
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result4 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"code": fragment_url},
        )
        await hass.async_block_till_done()

    assert result4["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    mock_auth.async_authorize.assert_called_once()
    call_args = mock_auth.async_authorize.call_args
    assert call_args[0][0] == "frag_code"


async def test_auth_step_url_without_code_anywhere(hass: HomeAssistant, mock_auth):
    """Test that a URL with no code param in query OR fragment returns invalid_auth."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await _navigate_to_auth_manual(hass, result["flow_id"])

    # URL that starts with http but has no 'code' anywhere
    bad_url = "http://example.com/callback?state=abc&other=value"
    result4 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": bad_url},
    )

    assert result4["type"] == data_entry_flow.FlowResultType.FORM
    assert result4["errors"]["base"] == "unknown"


# ---------------------------------------------------------------------------
# Duplicate prevention (unique_id)
# ---------------------------------------------------------------------------


async def test_user_step_aborts_if_already_configured(hass: HomeAssistant, mock_auth):
    """Adding the same App ID twice aborts as already_configured."""
    existing = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"next_step_id": "auth_manual"})
    result4 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"code": "auth_code_from_provider"},
    )

    assert result4["type"] == data_entry_flow.FlowResultType.ABORT
    assert result4["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reauth flow
# ---------------------------------------------------------------------------


async def test_auth_url_includes_flow_id(hass: HomeAssistant, mock_auth):
    """Test the authorization URL embeds the current config flow ID."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)

    mock_auth.auth_url.assert_called_once()
    redirect_uri = mock_auth.auth_url.call_args[0][0]
    assert f"flow_id={result['flow_id']}" in redirect_uri


async def test_auth_callback_step_registers_future(hass: HomeAssistant, mock_auth):
    """Test choosing automatic authorization registers a callback future."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)

    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "auth_callback"}
    )
    assert result3["step_id"] == "auth_callback"
    assert result3["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

    flows = hass.data[DOMAIN]["flows"]
    assert result["flow_id"] in flows
    assert isinstance(flows[result["flow_id"]], asyncio.Future)


async def test_callback_view_resumes_flow(hass: HomeAssistant, mock_auth):
    """Test the HTTP callback view delivers the code and completes the flow."""
    from custom_components.sungrow import SungrowAuthCallbackView

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)
    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "auth_callback"}
    )
    assert result3["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"code": "callback_code", "flow_id": result["flow_id"]}
    view = SungrowAuthCallbackView()

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        response = await view.get(request)
        # Wait for the progress task to resume and finish the flow.
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    assert response.status == 200
    mock_auth.async_authorize.assert_called_once_with("callback_code", MOCK_USER_INPUT["redirect_uri"])


async def test_callback_view_resumes_flow_without_flow_id(hass: HomeAssistant, mock_auth):
    """Callback with no flow_id falls back to the only pending future (#isolarcloud-strips-params)."""
    from custom_components.sungrow import SungrowAuthCallbackView

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)
    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "auth_callback"}
    )
    assert result3["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

    request = MagicMock()
    request.app = {"hass": hass}
    # iSolarCloud strips extra query params — flow_id is absent
    request.query = {"code": "callback_code"}
    view = SungrowAuthCallbackView()

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        response = await view.get(request)
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    assert response.status == 200
    mock_auth.async_authorize.assert_called_once_with("callback_code", MOCK_USER_INPUT["redirect_uri"])


async def test_callback_view_rejects_unknown_flow(hass: HomeAssistant, mock_auth):
    """Test the HTTP callback view rejects callbacks for unknown flows."""
    from custom_components.sungrow import SungrowAuthCallbackView

    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"code": "callback_code", "flow_id": "unknown-flow"}
    view = SungrowAuthCallbackView()

    response = await view.get(request)
    assert response.status == 400


async def test_auth_callback_timeout_aborts_flow(hass: HomeAssistant, mock_auth):
    """Test that a missing callback within the timeout aborts the flow."""
    from unittest.mock import patch

    async def _fake_wait_for(*args, **kwargs):
        """Yield control so the progress step is returned, then time out."""
        await asyncio.sleep(0)
        raise TimeoutError

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)

    with patch("custom_components.sungrow.config_flow.asyncio.wait_for", side_effect=_fake_wait_for):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "auth_callback"}
        )
        assert result3["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        # Wait for the background task to process the timeout.
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    # The flow should have been aborted by the timeout handler.
    with pytest.raises(data_entry_flow.UnknownFlow):
        hass.config_entries.flow.async_get(result["flow_id"])


async def test_reauth_flow_updates_entry(hass: HomeAssistant, mock_auth, mock_setup_auth, mock_plants_service):
    """Reauth re-runs authorization and updates the existing entry in place (#14)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "auth"

    # Choose manual entry and provide a fresh authorization code.
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "auth_manual"}
    )
    assert result2["step_id"] == "auth_manual"

    mock_auth.tokens = {"access_token": "fresh_token", "refresh_token": "fresh_refresh", "expires_at": 9999999999}
    result3 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"code": "fresh_code"})
    await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.ABORT
    assert result3["reason"] == "reauth_successful"
    assert entry.data["tokens"]["access_token"] == "fresh_token"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_sets_scan_interval(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """The options flow stores a custom polling interval (#21)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    result2 = await hass.config_entries.options.async_configure(result["flow_id"], user_input={CONF_SCAN_INTERVAL: 10})
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 10


async def test_options_flow_parses_extra_measure_points(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """The options flow parses the free-text extra measure point mapping."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: 5,
            CONF_EXTRA_MEASURE_POINTS: "99999=battery_charge_power, 99998=battery_discharge_power",
        },
    )
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EXTRA_MEASURE_POINTS] == {
        "99999": "battery_charge_power",
        "99998": "battery_discharge_power",
    }


async def test_options_flow_rejects_invalid_extra_measure_points(
    hass: HomeAssistant, mock_setup_auth, mock_plants_service
):
    """The options flow rejects malformed extra measure point input."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 5, CONF_EXTRA_MEASURE_POINTS: "not_a_mapping"},
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] is not None
