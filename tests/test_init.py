"""Tests for Sungrow component setup and the auth callback view."""

from unittest.mock import AsyncMock

from aiohttp.test_utils import make_mocked_request
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import (
    SungrowAuthCallbackView,
    async_setup,
)
from custom_components.sungrow.const import CONF_SCAN_INTERVAL, DOMAIN

from .conftest import MOCK_CONFIG_DATA

# ---------------------------------------------------------------------------
# async_setup (registers the HTTP callback view)
# ---------------------------------------------------------------------------


async def test_async_setup_registers_callback_view(hass: HomeAssistant):
    """Test async_setup registers the SungrowAuthCallbackView."""
    result = await async_setup(hass, {})

    assert result is True
    hass.http.register_view.assert_called_once()
    view_arg = hass.http.register_view.call_args[0][0]
    assert isinstance(view_arg, SungrowAuthCallbackView)


# ---------------------------------------------------------------------------
# async_setup_entry / async_unload_entry
# ---------------------------------------------------------------------------


async def test_async_setup_entry_success(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A successful setup stores coordinators and creates entities."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinators = entry_data["coordinators"]
    # MOCK_PLANT_LIST has two plants.
    assert len(coordinators) == 2
    # Entities were created for the data points.
    assert hass.states.async_all("sensor")


async def test_async_unload_entry(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Test successful unload removes stored data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_no_tokens_triggers_reauth(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Missing tokens should raise ConfigEntryAuthFailed (reauth)."""
    data = MOCK_CONFIG_DATA.copy()
    del data["tokens"]
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_entry_connection_error_is_retried(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A transient connection failure raises ConfigEntryNotReady (retry)."""
    mock_plants_service.async_get_plants = AsyncMock(side_effect=ConnectionError("network down"))

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_auth_error_triggers_reauth(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A failed token refresh (KeyError) raises ConfigEntryAuthFailed (reauth)."""
    # pysolarcloud raises KeyError when the refresh response has no access_token.
    mock_plants_service.async_get_plants = AsyncMock(side_effect=KeyError("access_token"))

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_options_change_reloads_entry(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Updating options should reload the entry and apply the new scan interval."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL: 15})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinators = entry_data["coordinators"]
    assert coordinators[0].update_interval.total_seconds() == 15


# ---------------------------------------------------------------------------
# SungrowAuthCallbackView
# ---------------------------------------------------------------------------


class TestSungrowAuthCallbackView:
    """Tests for the OAuth callback HTTP view."""

    def setup_method(self):
        self.view = SungrowAuthCallbackView()

    def test_view_properties(self):
        """Test view URL, name, and auth requirement."""
        assert self.view.url == "/api/sungrow_hass/callback"
        assert self.view.name == "api:sungrow_hass:callback"
        assert self.view.requires_auth is False

    async def test_callback_missing_code(self, hass: HomeAssistant):
        """Test callback returns 400 when code is missing."""
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?flow_id=abc")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400
        assert "Missing code" in response.text

    async def test_callback_missing_flow_id(self, hass: HomeAssistant):
        """Test callback with no flow_id and no pending flows returns 400.

        iSolarCloud strips extra query params from the redirect URI, so flow_id
        may be absent. When there are no pending flows the callback still 400s.
        """
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=abc")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400

    async def test_callback_missing_both_params(self, hass: HomeAssistant):
        """Test callback returns 400 when both params are missing."""
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400

    async def test_callback_success(self, hass: HomeAssistant):
        """Test a successful callback signals the waiting flow future."""
        import asyncio

        future: asyncio.Future[str] = asyncio.Future()
        hass.data.setdefault("sungrow", {})["flows"] = {"flow_abc": future}

        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=auth_code_123&flow_id=flow_abc")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 200
        assert "Authorization successful" in response.text
        assert future.done()
        assert future.result() == "auth_code_123"

    async def test_callback_flow_not_found(self, hass: HomeAssistant):
        """Test callback returns 400 when no pending flow future exists."""
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=auth_code&flow_id=bad_flow")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400
        assert "not found" in response.text.lower()
