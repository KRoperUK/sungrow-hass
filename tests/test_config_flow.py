"""Tests for the Sungrow iSolarCloud config flow.

The setup is two-phase: the user step creates the hub (credentials, no tokens),
then authorization is completed via a reauth flow (auto OAuth-callback wait with a
manual code/URL fallback). Creating the token-less hub registers the callback view
before any redirect, which is what fixes the first-install 404.
"""

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
# Helpers
# ---------------------------------------------------------------------------


def _hub_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured hub entry (credentials present) added to hass."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    return entry


async def _reauth_to_manual(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Start reauth and force the automatic callback wait to time out.

    Returns the reauth flow_id, positioned at the ``auth_manual`` form.
    """

    async def _timeout(*args, **kwargs):
        await asyncio.sleep(0)
        raise TimeoutError

    with patch("custom_components.sungrow.config_flow.asyncio.wait_for", side_effect=_timeout):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["step_id"] == "auth_manual"
    return result["flow_id"]


# ---------------------------------------------------------------------------
# Phase 1: User form -> creates the hub
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


async def test_user_step_creates_hub(hass: HomeAssistant, mock_auth):
    """Submitting credentials creates the hub entry (no tokens yet)."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["title"] == f"Sungrow {MOCK_USER_INPUT[CONF_APP_ID]}"
    # The hub is created without tokens — authorization happens afterwards.
    assert "tokens" not in result2["data"]
    assert result2["data"][CONF_APP_KEY] == MOCK_USER_INPUT[CONF_APP_KEY]
    # Setting up the hub registered the OAuth callback view (via async_setup),
    # so the redirect endpoint exists before any authorization attempt.
    assert hass.data[DOMAIN]["callback_view_registered"] is True


async def test_user_step_aborts_if_already_configured(hass: HomeAssistant, mock_auth):
    """Adding the same App ID twice aborts as already_configured (at the user step)."""
    _hub_entry(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=MOCK_USER_INPUT)

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Phase 2: Authorization via reauth — success paths
# ---------------------------------------------------------------------------


async def test_reauth_completes_with_manual_code(hass: HomeAssistant, mock_auth):
    """Reauth authorizes and updates the entry in place (#14)."""
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    mock_auth.tokens = {"access_token": "fresh_token", "refresh_token": "fresh_refresh", "expires_at": 9999999999}
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": "fresh_code"})
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["tokens"]["access_token"] == "fresh_token"


async def test_reauth_extracts_code_from_url(hass: HomeAssistant, mock_auth):
    """Pasting a full callback URL extracts the code automatically."""
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    callback_url = "http://homeassistant.local:8123/api/sungrow_hass/callback?code=extracted_code&flow_id=123"
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": callback_url})
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    mock_auth.async_authorize.assert_called_once()
    assert mock_auth.async_authorize.call_args[0][0] == "extracted_code"


async def test_reauth_code_in_fragment(hass: HomeAssistant, mock_auth):
    """A URL with the code in the fragment (SPA-style redirect) is parsed."""
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    fragment_url = "http://homeassistant.local:8123/callback#state=abc?code=frag_code"
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": fragment_url})
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_auth.async_authorize.call_args[0][0] == "frag_code"


# ---------------------------------------------------------------------------
# Phase 2: Authorization via reauth — error paths
# ---------------------------------------------------------------------------


async def test_reauth_url_without_code(hass: HomeAssistant, mock_auth):
    """A URL with no code in query OR fragment returns an error form."""
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    bad_url = "http://example.com/callback?state=abc&other=value"
    result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": bad_url})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "unknown"


async def test_reauth_no_tokens(hass: HomeAssistant, mock_auth_no_tokens):
    """Authorization that returns no tokens surfaces invalid_auth."""
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": "some_code"})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_reauth_connection_error(hass: HomeAssistant, mock_auth):
    """A connection error during authorization surfaces cannot_connect."""
    mock_auth.async_authorize = AsyncMock(side_effect=ClientError("Connection failed"))
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": "some_code"})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_reauth_unexpected_error(hass: HomeAssistant, mock_auth):
    """An unexpected exception during authorization surfaces unknown."""
    mock_auth.async_authorize = AsyncMock(side_effect=RuntimeError("Boom"))
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": "some_code"})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "unknown"


async def test_reauth_library_missing(hass: HomeAssistant):
    """Reauth aborts if the pysolarcloud library is unavailable."""
    entry = _hub_entry(hass)
    with patch("custom_components.sungrow.config_flow.Auth", None):
        result = await entry.start_reauth_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "library_missing"


# ---------------------------------------------------------------------------
# async_step_finish aborts (tested in isolation)
# ---------------------------------------------------------------------------


def _flow_at_finish(hass: HomeAssistant):
    """Build a config flow primed to run async_step_finish in isolation."""
    from custom_components.sungrow.config_flow import SungrowConfigFlow

    flow = SungrowConfigFlow()
    flow.hass = hass
    flow.init_info = dict(MOCK_USER_INPUT)
    flow.context = {}
    return flow


async def test_finish_step_library_missing(hass: HomeAssistant):
    """async_step_finish aborts with library_missing when the library is gone."""
    flow = _flow_at_finish(hass)
    flow.context["code"] = "some_code"
    with patch("custom_components.sungrow.config_flow.Auth", None):
        result = await flow.async_step_finish()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "library_missing"


async def test_finish_step_missing_code(hass: HomeAssistant, mock_auth):
    """async_step_finish aborts with missing_code when no code reached the step."""
    flow = _flow_at_finish(hass)
    # No "code" in context.
    result = await flow.async_step_finish()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "missing_code"


# ---------------------------------------------------------------------------
# Phase 2: the automatic OAuth-callback wait
# ---------------------------------------------------------------------------


async def test_reauth_auto_wait_registers_future(hass: HomeAssistant, mock_auth):
    """Reauth's automatic step shows progress and registers a callback future."""
    entry = _hub_entry(hass)
    result = await entry.start_reauth_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "auth_callback"

    flows = hass.data[DOMAIN]["flows"]
    assert result["flow_id"] in flows
    assert isinstance(flows[result["flow_id"]], asyncio.Future)


async def test_auth_url_uses_bare_redirect(hass: HomeAssistant, mock_auth):
    """The auth URL uses the bare redirect URI (no flow_id).

    iSolarCloud strips extra query params from the redirect and validates the
    token-exchange redirect_uri against the auth request's, so appending a flow_id
    would break the exchange. The callback view resolves the flow via its
    single-flow fallback instead.
    """
    entry = _hub_entry(hass)
    await entry.start_reauth_flow(hass)

    mock_auth.auth_url.assert_called_once()
    redirect_uri = mock_auth.auth_url.call_args[0][0]
    assert "flow_id=" not in redirect_uri
    assert redirect_uri == MOCK_USER_INPUT["redirect_uri"].rstrip("/")


async def test_token_exchange_redirect_matches_auth_request(hass: HomeAssistant, mock_auth):
    """Regression: the token exchange must use the same redirect_uri as the auth URL.

    A mismatch (previously the auth URL had a flow_id appended while the exchange
    used the bare URI) makes iSolarCloud reject the exchange with 'invalid
    authentication'.
    """
    entry = _hub_entry(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        await hass.config_entries.flow.async_configure(flow_id, user_input={"code": "abc"})
        await hass.async_block_till_done()

    auth_redirect = mock_auth.auth_url.call_args[0][0]
    exchange_redirect = mock_auth.async_authorize.call_args[0][1]
    assert exchange_redirect == auth_redirect


async def test_reauth_timeout_falls_back_to_manual(hass: HomeAssistant, mock_auth):
    """A missing callback within the timeout drops to manual code entry."""
    entry = _hub_entry(hass)
    # The helper asserts the flow lands on the auth_manual form.
    await _reauth_to_manual(hass, entry)


async def test_callback_view_resumes_reauth(hass: HomeAssistant, mock_auth):
    """The HTTP callback view delivers the code and completes the reauth flow."""
    from custom_components.sungrow import SungrowAuthCallbackView

    entry = _hub_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"code": "callback_code", "flow_id": result["flow_id"]}
    view = SungrowAuthCallbackView()

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        response = await view.get(request)
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    assert response.status == 200
    mock_auth.async_authorize.assert_called_once_with("callback_code", MOCK_USER_INPUT["redirect_uri"])


async def test_callback_view_resumes_reauth_without_flow_id(hass: HomeAssistant, mock_auth):
    """Callback with no flow_id falls back to the only pending future (iSolarCloud strips params)."""
    from custom_components.sungrow import SungrowAuthCallbackView

    entry = _hub_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS

    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"code": "callback_code"}  # flow_id absent
    view = SungrowAuthCallbackView()

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        response = await view.get(request)
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    assert response.status == 200
    mock_auth.async_authorize.assert_called_once_with("callback_code", MOCK_USER_INPUT["redirect_uri"])


async def test_callback_view_rejects_unknown_flow(hass: HomeAssistant, mock_auth):
    """The HTTP callback view rejects callbacks for unknown flows."""
    from custom_components.sungrow import SungrowAuthCallbackView

    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"code": "callback_code", "flow_id": "unknown-flow"}
    view = SungrowAuthCallbackView()

    response = await view.get(request)
    assert response.status == 400


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
            CONF_SCAN_INTERVAL: 30,
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
        user_input={CONF_SCAN_INTERVAL: 30, CONF_EXTRA_MEASURE_POINTS: "not_a_mapping"},
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] is not None
