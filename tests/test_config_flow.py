"""Tests for the Sungrow iSolarCloud config flow.

The setup is two-phase: the user step creates the hub (credentials, no tokens),
then authorization is completed via a reauth flow (auto OAuth-callback wait with a
manual code/URL fallback). Creating the token-less hub registers the callback view
before any redirect, which is what fixes the first-install 404.
"""

import asyncio
from ipaddress import ip_address
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.config_flow import _parse_winet_properties
from custom_components.sungrow.const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_GATEWAY,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_REDIRECT_URI,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DOMAIN,
    TRANSPORT_MODBUS_ONLY,
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


async def test_reauth_wrong_account_aborts(hass: HomeAssistant, mock_auth):
    """Re-authorizing when the App ID no longer matches the entry aborts (wrong account)."""
    # unique_id is the App ID; construct an entry whose stored App ID diverges from it.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_APP_ID: "a_different_app"},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)
    flow_id = await _reauth_to_manual(hass, entry)

    mock_auth.tokens = {"access_token": "t", "refresh_token": "r", "expires_at": 9999999999}
    result = await hass.config_entries.flow.async_configure(flow_id, user_input={"code": "c"})
    await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


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


async def test_late_callback_completes_from_manual_step(hass: HomeAssistant, mock_auth):
    """A redirect that lands while the user is on the manual form still completes the flow (#75)."""
    from custom_components.sungrow import SungrowAuthCallbackView

    entry = _hub_entry(hass)

    # Time out only the FIRST wait (the automatic one) so the flow reaches the manual
    # form; the manual re-armed waiter then waits normally for the late callback.
    real_wait_for = asyncio.wait_for
    calls = {"n": 0}

    async def _wait_for(aw, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(0)
            aw.cancel()  # real asyncio.wait_for cancels the awaited future on timeout
            raise TimeoutError
        return await real_wait_for(aw, timeout=None)

    with patch("custom_components.sungrow.config_flow.asyncio.wait_for", side_effect=_wait_for):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        flow = hass.config_entries.flow.async_get(result["flow_id"])
        assert flow["step_id"] == "auth_manual"

        # The OAuth redirect finally lands (flow_id stripped -> single-flow fallback).
        request = MagicMock()
        request.app = {"hass": hass}
        request.query = {"code": "late_code"}
        view = SungrowAuthCallbackView()

        with patch("custom_components.sungrow.async_setup_entry", return_value=True):
            response = await view.get(request)
            await hass.async_block_till_done()
            await hass.async_block_till_done()

    assert response.status == 200
    # The flow completed automatically without the user pasting anything.
    assert mock_auth.async_authorize.call_args[0][0] == "late_code"
    assert entry.data["tokens"]["access_token"] == "test_access_token"


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
# OAuth callback correlation by state (#116)
# ---------------------------------------------------------------------------


async def test_reauth_registers_state_for_correlation(hass: HomeAssistant, mock_auth):
    """The authorize URL carries a ``state`` mapped back to this flow (#116)."""
    entry = _hub_entry(hass)
    result = await entry.start_reauth_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
    states = hass.data[DOMAIN]["states"]
    assert states  # a state token was registered
    state_token = next(iter(states))
    # It correlates to this exact flow, and the URL the user visits carries it.
    assert states[state_token] == result["flow_id"]
    assert f"state={state_token}" in result["description_placeholders"]["auth_url"]


async def test_callback_view_resumes_reauth_by_state(hass: HomeAssistant, mock_auth):
    """A state-correlated callback (no flow_id) resumes the right reauth flow (#116)."""
    from custom_components.sungrow import SungrowAuthCallbackView

    entry = _hub_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
    state_token = next(iter(hass.data[DOMAIN]["states"]))

    request = MagicMock()
    request.app = {"hass": hass}
    # flow_id stripped by iSolarCloud, but the OAuth state survives.
    request.query = {"code": "state_code", "state": state_token}
    view = SungrowAuthCallbackView()

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        response = await view.get(request)
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    assert response.status == 200
    mock_auth.async_authorize.assert_called_once_with("state_code", MOCK_USER_INPUT["redirect_uri"])


async def test_abort_prunes_pending_future_and_state(hass: HomeAssistant, mock_auth):
    """Removing a flow prunes its pending future and state so nothing lingers (#116)."""
    entry = _hub_entry(hass)
    result = await entry.start_reauth_flow(hass)
    flow_id = result["flow_id"]
    assert flow_id in hass.data[DOMAIN]["flows"]
    state_token = next(iter(hass.data[DOMAIN]["states"]))

    # Aborting the flow triggers async_remove, which prunes the correlators.
    hass.config_entries.flow.async_abort(flow_id)
    await hass.async_block_till_done()

    assert flow_id not in hass.data[DOMAIN]["flows"]
    assert state_token not in hass.data[DOMAIN]["states"]


async def test_async_remove_cancels_pending_future(hass: HomeAssistant):
    """async_remove cancels a still-pending future so no waiter task leaks (#116)."""
    from custom_components.sungrow.config_flow import SungrowConfigFlow

    flow = SungrowConfigFlow()
    flow.hass = hass
    flow.flow_id = "flow_under_test"
    flow._state = "state_under_test"

    pending: asyncio.Future = asyncio.Future()
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data["flows"] = {"flow_under_test": pending}
    domain_data["states"] = {"state_under_test": "flow_under_test"}

    flow.async_remove()

    assert pending.cancelled()
    assert "flow_under_test" not in domain_data["flows"]
    assert "state_under_test" not in domain_data["states"]


# ---------------------------------------------------------------------------
# Reconfigure flow
# ---------------------------------------------------------------------------


async def test_reconfigure_shows_form(hass: HomeAssistant, mock_auth):
    """Reconfigure opens a form pre-filled with the current settings."""
    entry = _hub_entry(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


async def test_reconfigure_modbus_only_edits_host_not_credentials(hass: HomeAssistant):
    """Reconfiguring a Modbus-only entry edits the WiNet-S host — never cloud credentials (#159).

    Regression: a cloud-free local hub must not present the app key/secret/gateway form.
    """
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
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reconfigure_modbus"
        # Only the local host — none of the cloud credential fields.
        keys = {str(m.schema) for m in result["data_schema"].schema}
        assert keys == {CONF_MODBUS_HOST}

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "192.168.1.55"}
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MODBUS_HOST] == "192.168.1.55"


async def test_reconfigure_modbus_only_blank_keeps_current_host(hass: HomeAssistant):
    """Submitting a blank host on reconfigure leaves the existing WiNet-S host intact (#159)."""
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
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "   "}
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert entry.data[CONF_MODBUS_HOST] == "10.0.0.9"


async def test_reconfigure_updates_settings_and_reauthorizes(hass: HomeAssistant, mock_auth):
    """Reconfigure changes region/credentials (App ID fixed) and re-authorizes in place (#80)."""
    entry = _hub_entry(hass)  # gateway=Europe, app_id=test_app_id

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"

    async def _timeout(*args, **kwargs):
        await asyncio.sleep(0)
        raise TimeoutError

    new_input = {
        CONF_APP_KEY: "new_key",
        CONF_APP_SECRET: "new_secret",
        CONF_GATEWAY: "Australia",
        CONF_REDIRECT_URI: MOCK_USER_INPUT[CONF_REDIRECT_URI],
    }
    # Submitting drives to the OAuth wait; force it to time out -> manual entry.
    with patch("custom_components.sungrow.config_flow.asyncio.wait_for", side_effect=_timeout):
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=new_input)
        assert result2["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["step_id"] == "auth_manual"

    mock_auth.tokens = {"access_token": "reconf_token", "refresh_token": "r", "expires_at": 9999999999}
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result3 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"code": "reconf_code"})
        await hass.async_block_till_done()

    assert result3["type"] == data_entry_flow.FlowResultType.ABORT
    assert result3["reason"] == "reconfigure_successful"
    # Entry updated in place: new region + credentials, App ID preserved, fresh tokens.
    assert entry.data[CONF_GATEWAY] == "Australia"
    assert entry.data[CONF_APP_KEY] == "new_key"
    assert entry.data[CONF_APP_ID] == "test_app_id"
    assert entry.data["tokens"]["access_token"] == "reconf_token"


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


async def test_options_flow_stores_and_trims_modbus_host(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """The Modbus host option is stored, whitespace-trimmed; blank means cloud-only (#159)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 30, CONF_MODBUS_HOST: "  192.168.1.93  "},
    )
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MODBUS_HOST] == "192.168.1.93"


async def test_options_flow_modbus_only_hides_cloud_settings(hass: HomeAssistant):
    """A Modbus-only entry's options show only the local poll interval — no cloud fields (#159).

    Regression: the cloud iSolarCloud-quota description, extra measure points, per-device
    sensors and the "leave blank for cloud only" host field must not appear on a local entry.
    """
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
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "modbus_options"
        # Only the poll interval — none of the cloud-only settings.
        keys = {str(m.schema) for m in result["data_schema"].schema}
        assert keys == {CONF_SCAN_INTERVAL}

        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SCAN_INTERVAL: 15}
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["data"] == {CONF_SCAN_INTERVAL: 15}
    assert entry.options[CONF_SCAN_INTERVAL] == 15


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


# ---------------------------------------------------------------------------
# Zeroconf discovery -> standalone Modbus-only entry (#159)
# ---------------------------------------------------------------------------


def _winet_discovery(host: str = "192.168.1.93", serial: str = "A2340512345", model: str = "SG3.6RS"):
    """A WiNet-S mDNS discovery advertising one inverter's serial and model."""
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        port=80,
        hostname=f"SUNGROW{serial}.local.",
        type="_http._tcp.local.",
        name="WiNet-WebServer._http._tcp.local.",
        properties={
            "board": f"SUNGROW;1;WiNet-S;{serial};1;",
            "inverter": f"1;9732;{serial};1;516;{model};1;1;",
        },
    )


def test_parse_winet_properties():
    """The TXT-record parser extracts the inverter serial and model, or (None, None)."""
    assert _parse_winet_properties({"inverter": "1;9732;A2340512345;1;516;SG3.6RS;1;1;"}) == (
        "A2340512345",
        "SG3.6RS",
    )
    # bytes-valued TXT records are decoded.
    assert _parse_winet_properties({"inverter": b"1;9732;SN9;1;516;SG5.0RS;1;1;"}) == ("SN9", "SG5.0RS")
    # No inverter record -> nothing to identify.
    assert _parse_winet_properties({"board": "SUNGROW;1;WiNet-S;SN;1;"}) == (None, None)
    # Too few fields -> no crash, just None.
    assert _parse_winet_properties({"inverter": "1;9732"}) == (None, None)


async def test_zeroconf_discovery_creates_modbus_entry(hass: HomeAssistant):
    """Discovering a WiNet-S offers a confirm form, then creates a cloud-free Modbus entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_winet_discovery()
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {"model": "SG3.6RS", "host": "192.168.1.93"}

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["title"] == "Sungrow SG3.6RS (local)"
    assert result2["data"] == {
        CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
        CONF_SERIAL: "A2340512345",
        CONF_MODEL: "SG3.6RS",
        CONF_MODBUS_HOST: "192.168.1.93",
    }
    assert result2["options"] == {CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL}
    assert result2["result"].unique_id == "modbus_A2340512345"


async def test_zeroconf_aborts_non_sungrow_device(hass: HomeAssistant):
    """A discovery with no inverter serial is not a device we can set up."""
    info = _winet_discovery()
    info.properties.pop("inverter")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "not_sungrow_device"


async def test_zeroconf_aborts_when_already_configured_and_updates_host(hass: HomeAssistant):
    """A re-discovered WiNet-S aborts as already configured and refreshes its stored host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "A2340512345",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "192.168.1.50",  # old IP
        },
        unique_id="modbus_A2340512345",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_winet_discovery(host="192.168.1.99")
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The WiNet-S moved to a new DHCP lease; the stored host follows it.
    assert entry.data[CONF_MODBUS_HOST] == "192.168.1.99"
