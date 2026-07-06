"""Tests for Sungrow component setup and the auth callback view."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pysolarcloud
from aiohttp.test_utils import make_mocked_request
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pysolarcloud.plants import DeviceType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import (
    SungrowAuthCallbackView,
    SungrowData,
    async_remove_config_entry_device,
    async_setup,
    async_start_heartbeat,
    async_stop_heartbeat,
    resolve_point_device,
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


async def test_setup_entry_plants_timeout_is_retried(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A hung ``async_get_plants`` call times out and raises ConfigEntryNotReady (retry) (#115)."""

    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)

    mock_plants_service.async_get_plants = _hang

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    with patch("custom_components.sungrow.SETUP_TIMEOUT", 0.01):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_entry_auth_error_triggers_reauth(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A failed token refresh raises ConfigEntryAuthFailed (reauth)."""
    # pysolarcloud>=0.6.0 raises TokenRefreshError (error "token_refresh_failed")
    # when the refresh response has no access_token.
    mock_plants_service.async_get_plants = AsyncMock(
        side_effect=pysolarcloud.PySolarCloudException({"error": "token_refresh_failed"})
    )

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_partial_plant_failure_sets_up_remaining(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """One plant's transient first-refresh failure must not abort the others (#115)."""

    async def _realtime(plant_ids, **kwargs):
        if plant_ids == ["12345"]:
            raise ConnectionError("plant 12345 temporarily down")
        return MOCK_REALTIME_DATA

    mock_plants_service.async_get_realtime_data = AsyncMock(side_effect=_realtime)

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # Only the healthy plant got a coordinator; the failed one is skipped for now.
    coordinators = entry.runtime_data.coordinators
    assert [c.plant_id for c in coordinators] == ["67890"]
    assert "12345" not in entry.runtime_data.devices


async def test_setup_all_plants_failure_raises_not_ready(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """If every plant fails its first refresh, the whole entry retries (#115)."""
    mock_plants_service.async_get_realtime_data = AsyncMock(side_effect=ConnectionError("all plants down"))

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_plant_auth_failure_still_triggers_reauth(
    hass: HomeAssistant, mock_setup_auth, mock_plants_service
):
    """An auth failure during a plant's first refresh propagates as reauth, not a skip (#115)."""
    mock_plants_service.async_get_realtime_data = AsyncMock(
        side_effect=pysolarcloud.PySolarCloudException({"error": "invalid_token"})
    )

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_async_remove_config_entry_device(hass: HomeAssistant):
    """Stale devices can be removed from the UI; present ones are protected."""
    from types import SimpleNamespace

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    coordinator = MagicMock()
    coordinator.plant_id = "12345"
    coordinator.devices = [{"uuid": "dev-1"}, {"uuid": "dev-2"}]
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    # The plant device and a present device are still known -> not removable.
    plant = SimpleNamespace(identifiers={(DOMAIN, "12345")})
    present = SimpleNamespace(identifiers={(DOMAIN, "dev-1")})
    assert await async_remove_config_entry_device(hass, entry, plant) is False
    assert await async_remove_config_entry_device(hass, entry, present) is False

    # A device the API no longer reports -> removable.
    gone = SimpleNamespace(identifiers={(DOMAIN, "old-device")})
    assert await async_remove_config_entry_device(hass, entry, gone) is True


async def test_prune_stale_devices(hass: HomeAssistant):
    """Devices no longer reported by the API are pruned; present ones are kept."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.sungrow import _async_prune_stale_devices

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.plant_id = "12345"
    coordinator.devices = [{"uuid": "live-dev"}]
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    registry = dr.async_get(hass)
    plant = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "12345")})
    live = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "live-dev")})
    stale = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "old-dev")})

    _async_prune_stale_devices(hass, entry)

    # Plant device and the live device remain; the stale device is removed.
    assert registry.async_get(plant.id) is not None
    assert registry.async_get(live.id) is not None
    assert registry.async_get(stale.id) is None


async def test_options_change_reloads_entry(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Changing an option via the options flow reloads the entry (#110).

    ``OptionsFlowWithReload`` schedules the reload when the flow stores new
    options, so the fresh scan interval takes effect on a brand-new coordinator.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime_before = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], user_input={CONF_SCAN_INTERVAL: 15})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # A reload replaced runtime_data and applied the new interval.
    assert entry.runtime_data is not runtime_before
    coordinators = entry.runtime_data.coordinators
    assert coordinators[0].update_interval.total_seconds() == 15


async def test_token_write_does_not_reload_entry(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A token rotation (an ``entry.data`` write) must NOT reload the integration (#110).

    ``_save_tokens`` writes rotated tokens back to ``entry.data`` on every refresh.
    Only an options change should reload; a token-only write must leave the running
    integration untouched (no extra API calls, no brief unavailability, no cancelled
    EMS heartbeat), which we assert by the ``runtime_data`` object surviving intact.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime_before = entry.runtime_data

    rotated = {"access_token": "rotated", "refresh_token": "rotated_r", "token_type": "bearer"}
    hass.config_entries.async_update_entry(entry, data={**entry.data, "tokens": rotated})
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # No reload happened: the same runtime_data object, and the tokens were persisted.
    assert entry.runtime_data is runtime_before
    assert entry.data["tokens"] == rotated


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

    async def test_callback_resolves_by_state(self, hass: HomeAssistant):
        """A callback correlated by OAuth ``state`` resolves the right flow (#116)."""
        future: asyncio.Future[str] = asyncio.Future()
        domain_data = hass.data.setdefault(DOMAIN, {})
        domain_data["flows"] = {"flow_xyz": future}
        domain_data["states"] = {"state_token": "flow_xyz"}

        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=c&state=state_token")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 200
        assert future.result() == "c"

    async def test_callback_state_routes_to_correct_concurrent_flow(self, hass: HomeAssistant):
        """With two pending flows, a valid state routes the code to the right one (#116).

        The old single-flow fallback would 400 (``len(flows) != 1``); state
        correlation must resolve the matching flow and leave the other untouched.
        """
        future_a: asyncio.Future[str] = asyncio.Future()
        future_b: asyncio.Future[str] = asyncio.Future()
        domain_data = hass.data.setdefault(DOMAIN, {})
        domain_data["flows"] = {"flow_a": future_a, "flow_b": future_b}
        domain_data["states"] = {"s_b": "flow_b"}

        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=code_b&state=s_b")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 200
        assert future_b.result() == "code_b"
        assert not future_a.done()

    async def test_callback_unknown_correlator_does_not_misroute(self, hass: HomeAssistant):
        """An unknown state with several pending flows resolves nothing (#116).

        A stale/foreign correlator must never be dropped onto some other flow's
        future — the view returns 400 and leaves every pending future untouched.
        """
        future_a: asyncio.Future[str] = asyncio.Future()
        future_b: asyncio.Future[str] = asyncio.Future()
        domain_data = hass.data.setdefault(DOMAIN, {})
        domain_data["flows"] = {"flow_a": future_a, "flow_b": future_b}
        domain_data["states"] = {}

        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=c&state=nope")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400
        assert not future_a.done()
        assert not future_b.done()


# ---------------------------------------------------------------------------
# resolve_point_device (per-device modelling, #158)
# ---------------------------------------------------------------------------


def _dev(uuid, dtype):
    return {"uuid": uuid, "device_type": dtype}


def test_resolve_point_device_singular_rehomes():
    """A mapped point re-homes onto the single device of its type."""
    inv = _dev("inv-1", DeviceType.INVERTER)
    meter = _dev("m-1", DeviceType.METER)
    devices = [inv, meter]
    assert resolve_point_device("inverter_ac_power", devices) is inv
    assert resolve_point_device("grid_active_power", devices) is meter


def test_resolve_point_device_zero_or_multiple_stays_plant():
    """0 or >1 matching devices keep the point on the plant (aggregate stays correct)."""
    two_inv = [_dev("inv-1", DeviceType.INVERTER), _dev("inv-2", DeviceType.INVERTER)]
    assert resolve_point_device("inverter_ac_power", two_inv) is None
    assert resolve_point_device("battery_soc", [_dev("inv-1", DeviceType.INVERTER)]) is None


def test_resolve_point_device_unmapped_and_hybrid():
    """Unmapped codes stay on the plant; a hybrid ESS satisfies both PV and battery points."""
    assert resolve_point_device("load_power", [_dev("inv-1", DeviceType.INVERTER)]) is None
    ess = _dev("ess-1", DeviceType.ENERGY_STORAGE_SYSTEM)
    assert resolve_point_device("inverter_ac_power", [ess]) is ess
    assert resolve_point_device("battery_soc", [ess]) is ess


def test_resolve_point_device_ignores_uuidless():
    """A device without a uuid can't own a point."""
    assert resolve_point_device("inverter_ac_power", [{"device_type": DeviceType.INVERTER}]) is None
