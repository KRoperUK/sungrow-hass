"""Tests for Sungrow component setup and the auth callback view."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pysolarcloud
from aiohttp.test_utils import make_mocked_request
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import (
    SungrowAuthCallbackView,
    SungrowData,
    async_setup,
    async_start_heartbeat,
    async_stop_heartbeat,
)
from custom_components.sungrow.const import CONF_SCAN_INTERVAL, DOMAIN

from .conftest import MOCK_CONFIG_DATA, MOCK_PLANT_LIST, MOCK_REALTIME_DATA

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
    coordinators = entry.runtime_data.coordinators
    # MOCK_PLANT_LIST has two plants.
    assert len(coordinators) == 2
    # Entities were created for the data points.
    assert hass.states.async_all("sensor")


async def test_setup_persists_rotated_tokens(hass: HomeAssistant):
    """End-to-end: a token rotation during setup is written back to entry.data.

    This is the fix for #14/#15/#20/#21 — the ``_save_tokens`` callback wired into
    ``async_setup_entry`` must persist the rotated tokens so they survive a restart.
    Uses the real ``SungrowAuth`` (not the ``mock_setup_auth`` fixture) so the
    token_updater wiring is exercised for real.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    rotated = {"access_token": "new_access", "refresh_token": "new_refresh", "token_type": "bearer"}
    captured: dict = {}

    class _FakePlants:
        def __init__(self, auth):
            captured["auth"] = auth

        async def async_get_plants(self):
            # pysolarcloud refreshes the access token here and rotates the dict.
            await captured["auth"].async_get_access_token()
            return MOCK_PLANT_LIST

        async def async_get_realtime_data(self, *args, **kwargs):
            return MOCK_REALTIME_DATA

        async def async_get_plant_devices(self, *args, **kwargs):
            return []

    async def _fake_parent_get_token(self):
        # Simulate pysolarcloud assigning a brand-new tokens dict on refresh.
        self.tokens = rotated
        return "new_access"

    with (
        patch("custom_components.sungrow.Plants", _FakePlants),
        patch("custom_components.sungrow.Control", MagicMock()),
        patch("custom_components.sungrow.async_get_clientsession", return_value=MagicMock()),
        patch.object(pysolarcloud.Auth, "async_get_access_token", _fake_parent_get_token),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The rotated tokens must be persisted back to the entry, not just held in memory.
    assert entry.data["tokens"] == rotated


async def test_async_unload_entry(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Test successful unload removes stored data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


# ---------------------------------------------------------------------------
# Heartbeat lifecycle
# ---------------------------------------------------------------------------


def _entry_with_heartbeats(hass: HomeAssistant) -> MockConfigEntry:
    """Create a minimal entry whose heartbeat_loop waits on its stop event."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    control = MagicMock()
    # A realistic loop: block until the stop event is set.
    control.heartbeat_loop = lambda uuid, interval, stop_event: stop_event.wait()
    entry.runtime_data = SungrowData(coordinators=[], control=control, devices={})
    return entry


async def test_start_heartbeat_creates_tracked_task(hass: HomeAssistant):
    """Starting a heartbeat stores a (stop_event, task) pair and runs the loop."""
    entry = _entry_with_heartbeats(hass)
    await async_start_heartbeat(hass, entry, "12345", "dev-1", interval=60)

    heartbeats = entry.runtime_data.heartbeats
    assert "12345" in heartbeats
    stop_event, task = heartbeats["12345"]
    assert isinstance(stop_event, asyncio.Event)
    assert not task.done()

    await async_stop_heartbeat(hass, entry, "12345")
    assert stop_event.is_set()
    assert task.done()
    assert "12345" not in heartbeats


async def test_restart_heartbeat_stops_previous_loop(hass: HomeAssistant):
    """Restarting stops and awaits the previous loop before starting a new one (no double-run)."""
    entry = _entry_with_heartbeats(hass)
    await async_start_heartbeat(hass, entry, "12345", "dev-1", interval=60)
    first_event, first_task = entry.runtime_data.heartbeats["12345"]

    await async_start_heartbeat(hass, entry, "12345", "dev-1", interval=60)
    second_event, second_task = entry.runtime_data.heartbeats["12345"]

    assert first_event.is_set()
    assert first_task.done()
    assert second_task is not first_task
    assert not second_task.done()

    await async_stop_heartbeat(hass, entry, "12345")


async def test_stop_heartbeat_absent_is_noop(hass: HomeAssistant):
    """Stopping a heartbeat that isn't running does not raise."""
    entry = _entry_with_heartbeats(hass)
    await async_stop_heartbeat(hass, entry, "nonexistent")


async def test_stop_heartbeat_cancels_stubborn_loop(hass: HomeAssistant):
    """A loop that ignores its stop event is force-cancelled after the timeout."""
    entry = _entry_with_heartbeats(hass)

    async def _stubborn(uuid, interval, stop_event):
        await asyncio.sleep(3600)

    entry.runtime_data.control.heartbeat_loop = _stubborn

    with patch("custom_components.sungrow.HEARTBEAT_STOP_TIMEOUT", 0.01):
        await async_start_heartbeat(hass, entry, "12345", "dev-1", interval=60)
        _, task = entry.runtime_data.heartbeats["12345"]
        await async_stop_heartbeat(hass, entry, "12345")

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.done()


async def test_unload_cancels_running_heartbeat(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Unloading the entry signals and awaits any running heartbeat loop."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stop_event = asyncio.Event()
    task = entry.async_create_background_task(hass, stop_event.wait(), name="test-heartbeat")
    entry.runtime_data.heartbeats["12345"] = (stop_event, task)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert stop_event.is_set()
    assert task.done()


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
    coordinators = entry.runtime_data.coordinators
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

    async def test_callback_single_pending_flow_fallback(self, hass: HomeAssistant):
        """No flow_id but exactly one pending flow: resolve it.

        iSolarCloud strips extra query params from the redirect URI, so the real
        production callback usually has no flow_id. With a single pending flow the
        view falls back to it (the ``elif len(flows) == 1`` branch).
        """
        future: asyncio.Future[str] = asyncio.Future()
        hass.data.setdefault(DOMAIN, {})["flows"] = {"only_flow": future}

        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=the_code")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 200
        assert future.done()
        assert future.result() == "the_code"
