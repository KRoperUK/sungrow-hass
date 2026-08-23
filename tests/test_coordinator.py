"""Tests for the Sungrow data update coordinator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pysolarcloud import AuthError, PySolarCloudException
from pysolarcloud.plants import DeviceType

from custom_components.sungrow.const import (
    BATTERY_DEVICE_POINTS,
    COMM_MODULE_POINTS,
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODBUS_UNIT,
    CONF_SCAN_INTERVAL,
    CONF_TRANSPORT,
    DEVICE_REFRESH_INTERVAL,
    DOMAIN,
    ESS_BATTERY_POWER_POINTS,
    ESS_OPERATING_STATUS_POINT,
    INVERTER_DIAGNOSTIC_POINTS,
    INVERTER_OPERATING_STATUS_POINT,
    METER_DEVICE_POINTS,
    TRANSPORT_MODBUS_ONLY,
)
from custom_components.sungrow.coordinator import (
    BACKOFF_MAX_INTERVAL,
    SungrowPlantCoordinator,
    describe_api_error,
    is_auth_error,
)

from .conftest import MOCK_REALTIME_DATA


def _make_entry(options=None, data=None):
    entry = MagicMock()
    entry.options = options or {}
    entry.data = data or {}
    return entry


@pytest.fixture(autouse=True)
def _stub_periodic_refresh(request, monkeypatch):
    """Auto-stub the coordinator's device / plant-detail refresh methods.

    ``_async_update_data`` calls these on every successful poll. The narrower
    exception catches introduced by #350 no longer swallow the ``TypeError``
    raised when an under-stubbed ``MagicMock`` plants_service is awaited, so
    tests that don't care about periodic refresh would otherwise need to stub
    both methods on every mock. Instead, they no-op by default; tests that do
    test refresh behavior opt back into the real methods with
    ``@pytest.mark.real_refresh`` (see ``test_refresh_devices_*`` below).
    """
    if request.node.get_closest_marker("real_refresh"):
        return
    monkeypatch.setattr(SungrowPlantCoordinator, "_async_maybe_refresh_devices", AsyncMock(return_value=None))
    monkeypatch.setattr(SungrowPlantCoordinator, "_async_maybe_refresh_plant_detail", AsyncMock(return_value=None))


# ---------------------------------------------------------------------------
# is_auth_error
# ---------------------------------------------------------------------------


def test_is_auth_error_keyerror_is_transient():
    """A bare KeyError is not an auth error.

    pysolarcloud>=0.6.0 raises the typed TokenRefreshError (error
    ``token_refresh_failed``) on a failed refresh, so a raw KeyError is no longer
    an auth signal and is left to retry as a transient failure.
    """
    assert is_auth_error(KeyError("access_token")) is False
    assert is_auth_error(KeyError("some_other_key")) is False


def test_is_auth_error_known_pysolarcloud_error():
    """Known pysolarcloud auth error codes are treated as auth errors."""
    assert is_auth_error(PySolarCloudException({"error": "invalid_token"})) is True
    assert is_auth_error(PySolarCloudException({"error": "auth_not_initialised"})) is True


def test_is_auth_error_token_refresh_failed():
    """The typed token_refresh_failed error (pysolarcloud>=0.6 TokenRefreshError) is an auth error."""
    assert is_auth_error(PySolarCloudException({"error": "token_refresh_failed"})) is True


def test_is_auth_error_transient():
    """Transient errors are not auth errors."""
    assert is_auth_error(ConnectionError("boom")) is False
    assert is_auth_error(PySolarCloudException({"error": "server_busy"})) is False


@pytest.mark.parametrize("code", ["E00003", "E900", "E912", "E914"])
def test_is_auth_error_documented_reauth_codes(code):
    """Documented dead-credential codes are typed AuthError (0.9.0) and trigger reauth (#109/#131).

    Built via ``from_response`` — the real raise path — so the exception is an actual
    ``AuthError`` caught by ``isinstance``, not just a code-list match.
    """
    err = PySolarCloudException.from_response({"result_code": code})
    assert isinstance(err, AuthError)
    assert is_auth_error(err) is True


@pytest.mark.parametrize("code", ["E998", "E999"])
def test_is_auth_error_quota_codes_are_transient(code):
    """API quota/throttle codes (RateLimitError in 0.9.0) are transient, never reauth (#109)."""
    assert is_auth_error(PySolarCloudException.from_response({"result_code": code})) is False


@pytest.mark.parametrize("code", ["E918", "E919"])
def test_is_auth_error_whitelist_codes_are_transient(code):
    """Whitelist rejections (IP/user) are Developer-Portal config issues re-auth can't fix,
    so they must keep retrying rather than trigger reauth."""
    assert is_auth_error(PySolarCloudException({"error": code})) is False


def test_is_auth_error_e919_stays_transient_despite_autherror_typing():
    """pysolarcloud 0.9.0 types E919 as AuthError, but it's a whitelist rejection — reauth
    can't add a user to the whitelist, so it must stay transient (#131 must not regress #133)."""
    err = PySolarCloudException.from_response({"result_code": "E919"})
    assert isinstance(err, AuthError)  # the typing that would otherwise force reauth
    assert is_auth_error(err) is False  # ...guarded so it keeps retrying


def test_describe_api_error_hints():
    """Whitelist and quota codes get an actionable hint; other errors get none."""
    assert "IP whitelist" in (describe_api_error(PySolarCloudException({"error": "E918"})) or "")
    assert "user whitelist" in (describe_api_error(PySolarCloudException({"error": "E919"})) or "")
    assert "monthly" in (describe_api_error(PySolarCloudException({"error": "E998"})) or "")
    assert "hourly" in (describe_api_error(PySolarCloudException({"error": "E999"})) or "")
    assert describe_api_error(PySolarCloudException({"error": "E900"})) is None
    assert describe_api_error(ConnectionError("boom")) is None


def test_is_auth_error_unrelated_code_is_transient():
    """An unrelated/unknown error code is not an auth error."""
    assert is_auth_error(PySolarCloudException({"error": "E00007"})) is False


# ---------------------------------------------------------------------------
# _async_update_data
# ---------------------------------------------------------------------------


async def test_update_data_success(hass: HomeAssistant):
    """Successful data fetch returns plant data."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    data = await coordinator._async_update_data()

    assert data["total_active_power"]["value"] == "5.23"


async def test_update_data_missing_plant(hass: HomeAssistant):
    """Returns empty dict when the plant_id is not in the response."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"99999": {}})

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    assert await coordinator._async_update_data() == {}


async def test_update_data_transient_error_raises_update_failed(hass: HomeAssistant):
    """A transient API error raises UpdateFailed (HA retries)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=ConnectionError("API down"))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_whitelist_error_raises_repair_and_recovery_clears_it(hass: HomeAssistant):
    """A whitelist rejection raises a Repair; a later success clears it (#153)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "E918"}))
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "whitelist_rejection_12345") is not None

    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    await coordinator._async_update_data()
    assert registry.async_get_issue(DOMAIN, "whitelist_rejection_12345") is None


async def test_rate_limit_error_raises_its_repair_only(hass: HomeAssistant):
    """A rate-limit rejection raises the rate_limited Repair, not the whitelist one (#153)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "E999"}))
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "rate_limited_12345") is not None
    assert registry.async_get_issue(DOMAIN, "whitelist_rejection_12345") is None


async def test_rate_limit_backs_off_and_recovers(hass: HomeAssistant):
    """Rate-limit errors widen the poll interval; a success restores it (#156)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException.from_response({"result_code": "E999"}))
    coordinator = SungrowPlantCoordinator(hass, _make_entry({CONF_SCAN_INTERVAL: 300}), plants, "12345", "Test Plant")
    base = coordinator.update_interval

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == base * 2
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == base * 4

    # Recover.
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    await coordinator._async_update_data()
    assert coordinator.update_interval == base


async def test_rate_limit_backoff_is_capped(hass: HomeAssistant):
    """The backed-off interval never exceeds BACKOFF_MAX_INTERVAL (#156)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException.from_response({"result_code": "E998"}))
    coordinator = SungrowPlantCoordinator(hass, _make_entry({CONF_SCAN_INTERVAL: 300}), plants, "12345", "Test Plant")
    for _ in range(20):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert coordinator.update_interval == BACKOFF_MAX_INTERVAL


async def test_non_rate_limit_error_does_not_back_off(hass: HomeAssistant):
    """A plain connection error leaves the poll interval untouched (#156)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=ConnectionError("blip"))
    coordinator = SungrowPlantCoordinator(hass, _make_entry({CONF_SCAN_INTERVAL: 300}), plants, "12345", "Test Plant")
    base = coordinator.update_interval

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.update_interval == base


async def test_transient_error_preserves_rate_limit_backoff_and_repair(hass: HomeAssistant):
    """A transient error during an active rate-limit must not reset the backoff or clear the Repair.

    Regression: a single interleaved ConnectionError/TimeoutError used to snap the
    interval back to base and dismiss the rate_limited Repair, so the integration
    immediately resumed 5-minute polling and re-tripped the quota (#153/#156).
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException.from_response({"result_code": "E999"}))
    coordinator = SungrowPlantCoordinator(hass, _make_entry({CONF_SCAN_INTERVAL: 300}), plants, "12345", "Test Plant")
    base = coordinator.update_interval
    registry = ir.async_get(hass)

    # Rate-limited: back off and raise the Repair.
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == base * 2
    assert registry.async_get_issue(DOMAIN, "rate_limited_12345") is not None

    # A transient network error must NOT undo the backoff or clear the Repair.
    plants.async_get_realtime_data = AsyncMock(side_effect=ConnectionError("blip"))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == base * 2
    assert registry.async_get_issue(DOMAIN, "rate_limited_12345") is not None

    # Only a real success restores the interval and clears the Repair.
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    await coordinator._async_update_data()
    assert coordinator.update_interval == base
    assert registry.async_get_issue(DOMAIN, "rate_limited_12345") is None


async def test_transient_failure_keeps_last_good_within_grace(hass: HomeAssistant):
    """A brief poll failure keeps the last-good data instead of flapping unavailable (#152)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    good = coordinator.data

    # The next poll fails transiently, within the grace window.
    plants.async_get_realtime_data = AsyncMock(side_effect=ConnectionError("blip"))
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True  # stayed available
    assert coordinator.data == good  # served the last-good data


async def test_transient_failure_raises_after_grace(hass: HomeAssistant):
    """Once the last success is older than the grace window, entities go unavailable (#152)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True

    # Pretend the last success was long ago, then fail.
    coordinator._last_successful_update = hass.loop.time() - 100000
    plants.async_get_realtime_data = AsyncMock(side_effect=ConnectionError("down"))
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_auth_error_not_debounced_within_grace(hass: HomeAssistant):
    """An auth error still triggers reauth immediately, even inside the grace window (#152)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    await coordinator.async_refresh()  # success -> within grace, has last-good data

    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "invalid_token"}))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_data_auth_error_raises_config_entry_auth_failed(hass: HomeAssistant):
    """A failed token refresh raises ConfigEntryAuthFailed (triggers reauth)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "token_refresh_failed"}))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_data_whitelist_error_surfaces_hint(hass: HomeAssistant):
    """An IP-whitelist rejection retries (UpdateFailed) with an actionable message, not reauth."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "E918"}))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(UpdateFailed, match="IP whitelist"):
        await coordinator._async_update_data()


async def test_update_data_pysolarcloud_auth_error_raises_config_entry_auth_failed(hass: HomeAssistant):
    """A pysolarcloud invalid_token error raises ConfigEntryAuthFailed."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "invalid_token"}))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_data_pysolarcloud_transient_error_raises_update_failed(hass: HomeAssistant):
    """A pysolarcloud server_busy error raises UpdateFailed."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=PySolarCloudException({"error": "server_busy"}))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_data_keyerror_raises_update_failed(hass: HomeAssistant):
    """A bare KeyError is treated as a transient UpdateFailed, not an auth error."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=KeyError("some_other_key"))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# Device list refresh (dynamic / stale devices)
# ---------------------------------------------------------------------------


@pytest.mark.real_refresh
async def test_refresh_devices_updates_list(hass: HomeAssistant):
    """Each poll refreshes the plant's device list so new devices are picked up."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})
    plants.async_get_plant_devices = AsyncMock(return_value=[{"uuid": "new-dev"}])
    plants.async_get_plant_details = AsyncMock(return_value=[])

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices=[])
    await coordinator._async_update_data()

    assert coordinator.devices == [{"uuid": "new-dev"}]


@pytest.mark.real_refresh
async def test_refresh_devices_failure_keeps_previous_list(hass: HomeAssistant):
    """A device-list fetch failure keeps the previous list (best effort, non-fatal)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})
    plants.async_get_plant_devices = AsyncMock(side_effect=ClientError("boom"))
    plants.async_get_plant_details = AsyncMock(return_value=[])

    coordinator = SungrowPlantCoordinator(
        hass, _make_entry(), plants, "12345", "Test Plant", devices=[{"uuid": "keep"}]
    )
    await coordinator._async_update_data()

    assert coordinator.devices == [{"uuid": "keep"}]


@pytest.mark.real_refresh
async def test_device_list_refresh_is_throttled(hass: HomeAssistant):
    """The device list is refreshed on the first poll, then only once the interval elapses (#115)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})
    plants.async_get_plant_devices = AsyncMock(return_value=[{"uuid": "dev"}])
    plants.async_get_plant_details = AsyncMock(return_value=[])

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices=[])

    # First poll always refreshes.
    await coordinator._async_update_data()
    assert plants.async_get_plant_devices.await_count == 1

    # A second poll within the interval does NOT re-fetch the device list.
    await coordinator._async_update_data()
    assert plants.async_get_plant_devices.await_count == 1

    # Once the interval has elapsed, the next poll refreshes again.
    coordinator._last_device_refresh -= DEVICE_REFRESH_INTERVAL + 1
    await coordinator._async_update_data()
    assert plants.async_get_plant_devices.await_count == 2


async def test_poll_timeout_raises_update_failed(hass: HomeAssistant):
    """A hung realtime request times out and surfaces as a transient UpdateFailed (#115)."""

    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)

    plants = MagicMock()
    plants.async_get_realtime_data = _hang

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    coordinator._poll_timeout = 0.01
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# scan interval from options
# ---------------------------------------------------------------------------


async def test_scan_interval_uses_default(hass: HomeAssistant):
    """Default scan interval is 300 seconds."""
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), MagicMock(), "1", "P")
    assert coordinator.update_interval.total_seconds() == 300


async def test_scan_interval_from_options(hass: HomeAssistant):
    """Scan interval option (in seconds) overrides the default."""
    entry = _make_entry({CONF_SCAN_INTERVAL: 30})
    coordinator = SungrowPlantCoordinator(hass, entry, MagicMock(), "1", "P")
    assert coordinator.update_interval.total_seconds() == 30


async def test_extra_measure_points_passed_to_api(hass: HomeAssistant):
    """Extra measure points from options are forwarded to pysolarcloud."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})

    entry = _make_entry({CONF_EXTRA_MEASURE_POINTS: {"99999": "battery_charge_power"}})
    coordinator = SungrowPlantCoordinator(hass, entry, plants, "12345", "Test Plant")
    await coordinator._async_update_data()

    plants.async_get_realtime_data.assert_awaited_once_with(
        ["12345"], extra_measure_points={"99999": "battery_charge_power"}
    )


async def test_extra_measure_points_empty_passes_none(hass: HomeAssistant):
    """When no extra measure points are configured, None is passed to the API."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    await coordinator._async_update_data()

    plants.async_get_realtime_data.assert_awaited_once_with(["12345"], extra_measure_points=None)


# ---------------------------------------------------------------------------
# Per-device realtime (issue #74)
# ---------------------------------------------------------------------------


async def test_device_data_fetched_when_enabled(hass: HomeAssistant):
    """With the option on, per-device realtime is fetched and stored on the coordinator."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(
        return_value={"chg-1": {"ev_charger_power": {"code": "ev_charger_power", "value": "7.2"}}}
    )
    devices = [{"uuid": "chg-1", "device_type": 999}]  # unknown type left as raw int
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    assert coordinator.device_data["chg-1"]["ev_charger_power"]["value"] == "7.2"
    plants.async_get_device_realtime.assert_awaited_once()


async def test_device_data_not_fetched_when_disabled(hass: HomeAssistant):
    """With the option off, a non-inverter device (no operating status) isn't requested."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "chg-1", "device_type": 999}]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    assert coordinator.device_data == {}
    plants.async_get_device_realtime.assert_not_awaited()


async def test_operating_status_fetched_for_inverter_when_disabled(hass: HomeAssistant):
    """Even with device sensors off, an inverter's operating status is fetched (#182).

    This is what lets the Fault binary sensor show a reason without the full device-
    sensor set enabled; only the single operating-status point is requested.
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(
        return_value={"inv-1": {"operating_status": {"id": "29", "code": "operating_status", "value": "64"}}}
    )
    devices = [{"uuid": "inv-1", "device_type": DeviceType.INVERTER, "ps_key": "12345_1_1_1"}]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra == {"29": "operating_status"}  # only operating status, none of the heavy sets
    assert coordinator.device_data["inv-1"]["operating_status"]["value"] == "64"


async def test_operating_status_uses_13146_for_ess_when_disabled(hass: HomeAssistant):
    """ESS/hybrid operating status is requested on point 13146, not the inverter point (#182).

    Battery charge/discharge power points are also always requested (#31).
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "ps_key": "12345_14_1_1"}]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra == {
        "13146": "operating_status",
        "13126": "battery_charge_power",
        "13150": "battery_discharge_power",
    }


async def test_ess_operating_status_avoids_point29_collision(hass: HomeAssistant):
    """An ESS with device sensors ON requests 13146 for operating status, not point 29.

    Both 29 and 13146 map to the "operating_status" code, so requesting both would let
    one silently overwrite the other in the per-device merge, flapping the reason (#182).
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "ps_key": "12345_14_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["13146"] == "operating_status"
    assert "29" not in extra  # inverter operating-status point dropped for ESS (no collision)
    # The rest of the inverter diagnostic set is still requested.
    assert extra["14"] == "total_dc_power"


async def test_hybrid_typed_as_inverter_requests_battery_power(hass: HomeAssistant):
    """A hybrid the cloud types as a plain INVERTER still gets battery power points (#251).

    The model code (SH10RT-20) says it has a battery, so charge/discharge power is
    requested even though the device type is INVERTER and per-device sensors are off.
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [
        {
            "uuid": "inv-1",
            "device_type": DeviceType.INVERTER,
            "device_model_code": "SH10RT-20",
            "ps_key": "12345_1_1_1",
        }
    ]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["29"] == "operating_status"  # inverter-typed -> inverter status point
    assert extra["13126"] == "battery_charge_power"
    assert extra["13150"] == "battery_discharge_power"


async def test_string_inverter_never_requests_battery_power(hass: HomeAssistant):
    """An SG string inverter (no battery) is not asked for battery power points (#251)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [
        {
            "uuid": "inv-1",
            "device_type": DeviceType.INVERTER,
            "device_model_code": "SG3.6RS",
            "ps_key": "12345_1_1_1",
        }
    ]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra == {"29": "operating_status"}
    assert "13126" not in extra
    assert "13150" not in extra


async def test_sh_model_uses_hybrid_mppt_range(hass: HomeAssistant):
    """An SH hybrid (even typed INVERTER) requests the hybrid MPPT range, not points 5-10 (#251)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [
        {
            "uuid": "inv-1",
            "device_type": DeviceType.INVERTER,
            "device_model_code": "SH10RT-20",
            "ps_key": "12345_1_1_1",
        }
    ]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    # Hybrid MPPT range (13xxx) is requested; the string-inverter ids (5-10) are dropped.
    assert extra["13001"] == "mppt1_voltage"
    assert "5" not in extra
    assert "7" not in extra
    # Battery device points are requested too (model has a battery).
    assert extra["58604"] == "battery_level"


async def test_sg_model_uses_string_mppt_range(hass: HomeAssistant):
    """An SG string inverter keeps the string MPPT range (points 5-10), not the 13xxx range (#251)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [
        {
            "uuid": "inv-1",
            "device_type": DeviceType.INVERTER,
            "device_model_code": "SG3.6RS",
            "ps_key": "12345_1_1_1",
        }
    ]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["5"] == "mppt1_voltage"
    assert extra["7"] == "mppt2_voltage"
    assert "13001" not in extra
    # No battery device points for a string inverter.
    assert "58604" not in extra


async def test_device_data_best_effort_on_error(hass: HomeAssistant):
    """A failing device type is skipped without failing the whole update."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)

    async def _realtime(plant_id, device_type, **kwargs):
        if getattr(device_type, "value", device_type) == 999:
            raise ClientError("charger endpoint down")
        return {"inv-1": {"foo": {"code": "foo", "value": "1"}}}

    plants.async_get_device_realtime = AsyncMock(side_effect=_realtime)
    devices = [
        {"uuid": "inv-1", "device_type": DeviceType.INVERTER},
        {"uuid": "chg-1", "device_type": 999},
    ]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    # Must not raise even though one device type errors.
    await coordinator._async_update_data()

    assert "inv-1" in coordinator.device_data
    assert "chg-1" not in coordinator.device_data


async def test_device_data_forwards_ps_key_list(hass: HomeAssistant):
    """Each device's ps_key is forwarded so getDeviceRealTimeData isn't rejected (009)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [
        {"uuid": "inv-1", "device_type": DeviceType.INVERTER, "ps_key": "12345_1_1_1"},
        {"uuid": "inv-2", "device_type": DeviceType.INVERTER, "ps_key": "12345_1_1_2"},
    ]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    plants.async_get_device_realtime.assert_awaited_once()
    assert plants.async_get_device_realtime.await_args.kwargs["ps_key_list"] == ["12345_1_1_1", "12345_1_1_2"]


async def test_device_data_requests_inverter_diagnostic_points(hass: HomeAssistant):
    """Inverter/ESS device realtime is asked for the diagnostic points (#149)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "inv-1", "device_type": DeviceType.INVERTER, "ps_key": "12345_1_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["29"] == "operating_status"
    assert extra["14"] == "total_dc_power"
    assert "5" in extra  # MPPT1 voltage
    # Grid-side health points added in #179.
    assert extra["18"] == "phase_a_voltage"
    assert extra["26"] == "power_factor"
    assert extra["95"] == "bus_voltage"


async def test_device_data_diagnostic_points_only_for_inverter(hass: HomeAssistant):
    """A non-inverter device is not asked for inverter diagnostic points (#149)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "chg-1", "device_type": 999, "ps_key": "12345_999_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra is None or "29" not in extra


async def test_device_data_requests_battery_points_for_battery_device(hass: HomeAssistant):
    """A battery device is asked for the battery points, not the inverter-only ones (#154)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "bat-1", "device_type": DeviceType.BATTERY, "ps_key": "12345_43_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["58604"] == "battery_level"
    assert extra["58606"] == "battery_total_charge_energy"
    assert "29" not in extra  # not the inverter operating-status point


async def test_device_data_requests_meter_points_for_meter_device(hass: HomeAssistant):
    """A meter device is asked for the meter points, not the inverter/battery ones (#179)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "meter-1", "device_type": DeviceType.METER, "ps_key": "12345_7_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["8018"] == "meter_active_power"
    assert extra["8064"] == "meter_frequency"
    assert "29" not in extra  # not the inverter operating-status point
    assert "58604" not in extra  # not the battery points


async def test_device_data_ess_gets_both_inverter_and_battery_points(hass: HomeAssistant):
    """An ESS device reports both the inverter and the battery points (#149/#154)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "ps_key": "12345_14_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert "13146" in extra  # ESS operating status (inverter point 29 dropped to avoid a collision)
    assert "29" not in extra
    assert "14" in extra  # inverter DC power — the rest of the diagnostic set still applies
    assert "58604" in extra  # battery level


async def test_device_data_requests_comm_module_points(hass: HomeAssistant):
    """A communication module is asked for the WLAN/signal points (#149)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "comm-1", "device_type": DeviceType.COMMUNICATION_MODULE, "ps_key": "12345_22_247_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra["23014"] == "wlan_signal_strength"
    assert "29" not in extra  # not the inverter points


async def test_device_data_dedupes_device_types(hass: HomeAssistant):
    """Two devices of the same type trigger a single per-type fetch."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [
        {"uuid": "inv-1", "device_type": DeviceType.INVERTER},
        {"uuid": "inv-2", "device_type": DeviceType.INVERTER},
    ]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    plants.async_get_device_realtime.assert_awaited_once()


# ---------------------------------------------------------------------------
# Plant detail (#178)
# ---------------------------------------------------------------------------


@pytest.mark.real_refresh
async def test_plant_detail_fetched_and_stored(hass: HomeAssistant):
    """The plant-detail fields are fetched during a poll and stored on the coordinator (#178)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_plant_devices = AsyncMock(return_value=[])
    plants.async_get_plant_details = AsyncMock(
        return_value=[{"alarm_count": 0, "fault_count": 1, "install_power": 3600.0, "power_price_unit": "GBP"}]
    )
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", [])

    await coordinator._async_update_data()

    assert coordinator.plant_detail["install_power"] == 3600.0
    assert coordinator.plant_detail["fault_count"] == 1
    plants.async_get_plant_details.assert_awaited()


@pytest.mark.real_refresh
async def test_plant_detail_best_effort_on_error(hass: HomeAssistant):
    """A failing plant-detail fetch doesn't fail the whole poll (#178)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_plant_devices = AsyncMock(return_value=[])
    plants.async_get_plant_details = AsyncMock(side_effect=ClientError("detail endpoint down"))
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", [])

    await coordinator._async_update_data()  # must not raise

    assert coordinator.plant_detail == {}


# ---------------------------------------------------------------------------
# Local Modbus transport (#159)
# ---------------------------------------------------------------------------


async def test_build_modbus_client_only_for_modbus_only_entry(hass: HomeAssistant):
    """Modbus client is built for transport=modbus_only with a host, never for cloud entries."""
    from custom_components.sungrow.const import CONF_MODEL

    local = _make_entry(
        options={CONF_MODBUS_PORT: 1502, CONF_MODBUS_UNIT: 2},
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_MODBUS_HOST: "10.0.0.5",
        },
    )
    client = SungrowPlantCoordinator._build_modbus_client(local)
    assert client is not None
    assert (client.host, client.port, client.unit) == ("10.0.0.5", 1502, 2)
    assert client.model == "sg_rs"  # unknown/blank model → default

    hybrid = _make_entry(
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_MODBUS_HOST: "10.0.0.6",
            CONF_MODEL: "SH10RT-20",
        }
    )
    hybrid_client = SungrowPlantCoordinator._build_modbus_client(hybrid)
    assert hybrid_client is not None
    assert hybrid_client.model == "sh_rt"  # model string seeds the register map (#219)
    assert hybrid_client._configured_model == "SH10RT-20"  # noqa: SLF001

    # Cloud entry with leftover hybrid host must NOT get a client.
    hybrid_leftover = _make_entry({CONF_MODBUS_HOST: "10.0.0.5"})
    assert SungrowPlantCoordinator._build_modbus_client(hybrid_leftover) is None


def test_no_modbus_client_when_host_blank():
    """No host (or a blank host) means no Modbus client on a local entry."""
    assert SungrowPlantCoordinator._build_modbus_client(_make_entry()) is None
    assert (
        SungrowPlantCoordinator._build_modbus_client(
            _make_entry(data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, CONF_MODBUS_HOST: ""})
        )
        is None
    )


async def test_close_modbus_releases_client(hass: HomeAssistant):
    """close_modbus closes the underlying client and clears the coordinator handle."""
    from custom_components.sungrow.const import CONF_MODEL

    entry = _make_entry(
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_MODBUS_HOST: "10.0.0.5",
            CONF_MODEL: "SG3.6RS",
        }
    )
    mock_client = MagicMock()
    with patch("custom_components.sungrow.modbus.SungrowModbusClient", return_value=mock_client):
        coordinator = SungrowPlantCoordinator(hass, entry, None, "p1", "Plant", [])
    client = MagicMock()
    coordinator._modbus_client = client  # noqa: SLF001
    coordinator.close_modbus()
    client.close.assert_called_once()
    assert coordinator._modbus_client is None  # noqa: SLF001
    # Idempotent when already closed.
    coordinator.close_modbus()


async def test_modbus_debug_daily_yield_gated(hass: HomeAssistant):
    """Raw daily_yield register dump is only captured when the debug option is on."""
    entry = _make_entry(data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, CONF_MODBUS_HOST: "10.0.0.9"})
    coordinator = SungrowPlantCoordinator(hass, entry, None, "SN", "SG")
    coordinator._modbus_client = MagicMock()
    coordinator._modbus_client.async_read_daily_yield_diagnostic = AsyncMock(return_value={"raw": {"5002": 1}})

    # Default: off → clear / skip capture.
    await coordinator._async_capture_daily_yield_diagnostic()
    assert coordinator.daily_yield_diagnostic is None
    coordinator._modbus_client.async_read_daily_yield_diagnostic.assert_not_awaited()

    # Opt-in.
    entry.options = {CONF_MODBUS_DEBUG_DAILY_YIELD: True}
    await coordinator._async_capture_daily_yield_diagnostic()
    assert coordinator.daily_yield_diagnostic == {"raw": {"5002": 1}}


async def test_modbus_derives_daily_yield_from_total(hass: HomeAssistant):
    """When Modbus supplies total_yield, daily_yield is total − start-of-day baseline (#223)."""
    from datetime import date
    from unittest.mock import patch

    from custom_components.sungrow.daily_yield import DailyYieldBaseline

    entry = _make_entry(data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, CONF_MODBUS_HOST: "10.0.0.9"})
    coordinator = SungrowPlantCoordinator(hass, entry, None, "SN-DY", "SG")
    coordinator._modbus_client = MagicMock()
    # SG-RS wire 5002 is the register that never resets, so this family derives (#382).
    coordinator._modbus_client.model = "sg_rs"
    coordinator._modbus_client.async_read_realtime = AsyncMock(
        return_value={
            "total_yield": {"code": "total_yield", "value": 6467.0, "unit": "kWh", "source": "modbus"},
            "daily_yield": {"code": "daily_yield", "value": 201.6, "unit": "kWh", "source": "modbus"},
        }
    )
    # Pretend we already saw 6462 earlier today.
    coordinator._daily_yield_baseline_loaded = True
    coordinator._daily_yield_state = DailyYieldBaseline(
        baseline=6462.0, baseline_date=date(2026, 7, 13), last_total=6462.0
    )
    # Avoid Store I/O in the unit test.
    coordinator._daily_yield_store = MagicMock()
    coordinator._daily_yield_store.async_save = AsyncMock()
    coordinator._modbus_client.async_read_daily_yield_diagnostic = AsyncMock(return_value=None)

    with patch("custom_components.sungrow.coordinator.dt_util") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 7, 13)
        data = await coordinator._async_modbus_only_update()

    assert data["daily_yield"]["value"] == 5.0
    assert data["daily_yield"]["source"] == "modbus_derived"
    assert data["total_yield"]["value"] == 6467.0
    coordinator._daily_yield_store.async_save.assert_awaited()


async def test_modbus_sh_keeps_raw_daily_yield(hass: HomeAssistant):
    """SH hybrids reset wire 13001 nightly, so the raw register is kept as-is (#382).

    Deriving here would under-report until the first midnight after install, because
    the persisted baseline starts at whatever the lifetime total happened to be.
    """
    from datetime import date
    from unittest.mock import patch

    from custom_components.sungrow.daily_yield import DailyYieldBaseline

    entry = _make_entry(data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, CONF_MODBUS_HOST: "10.0.0.9"})
    coordinator = SungrowPlantCoordinator(hass, entry, None, "SN-SH", "SH")
    coordinator._modbus_client = MagicMock()
    coordinator._modbus_client.model = "sh_rt"
    coordinator._modbus_client.async_read_realtime = AsyncMock(
        return_value={
            "total_yield": {"code": "total_yield", "value": 1492.0, "unit": "kWh", "source": "modbus"},
            "daily_yield": {"code": "daily_yield", "value": 31.3, "unit": "kWh", "source": "modbus"},
        }
    )
    coordinator._daily_yield_baseline_loaded = True
    coordinator._daily_yield_state = DailyYieldBaseline(
        baseline=1400.0, baseline_date=date(2026, 7, 13), last_total=1400.0
    )
    coordinator._daily_yield_store = MagicMock()
    coordinator._daily_yield_store.async_save = AsyncMock()
    coordinator._modbus_client.async_read_daily_yield_diagnostic = AsyncMock(return_value=None)

    with patch("custom_components.sungrow.coordinator.dt_util") as mock_dt:
        mock_dt.now.return_value.date.return_value = date(2026, 7, 13)
        data = await coordinator._async_modbus_only_update()

    # Raw register value survives; no derived override, no baseline write.
    assert data["daily_yield"]["value"] == 31.3
    assert data["daily_yield"]["source"] == "modbus"
    coordinator._daily_yield_store.async_save.assert_not_awaited()


# ---------------------------------------------------------------------------
# Modbus-only transport (cloud-free entry, #159)
# ---------------------------------------------------------------------------


def _modbus_only_coordinator(hass: HomeAssistant) -> SungrowPlantCoordinator:
    """A coordinator with no cloud service and a stubbed Modbus client."""
    entry = _make_entry(data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, CONF_MODBUS_HOST: "10.0.0.9"})
    coordinator = SungrowPlantCoordinator(hass, entry, None, "SN123", "Sungrow SG3.6RS")
    assert coordinator.plants_service is None
    coordinator._modbus_client = MagicMock()
    return coordinator


async def test_modbus_only_update_reads_from_modbus(hass: HomeAssistant):
    """A Modbus-only entry sources all realtime data from the local client."""
    coordinator = _modbus_only_coordinator(hass)
    local = {"grid_frequency": {"code": "grid_frequency", "value": 49.9, "unit": "Hz", "source": "modbus"}}
    coordinator._modbus_client.async_read_realtime = AsyncMock(return_value=local)

    data = await coordinator._async_update_data()

    assert data == local
    assert coordinator._last_successful_update is not None


async def test_modbus_only_update_no_client_raises(hass: HomeAssistant):
    """A Modbus-only entry with no client configured fails the update (defensive)."""
    entry = _make_entry()  # no host anywhere -> no client is built
    coordinator = SungrowPlantCoordinator(hass, entry, None, "SN123", "Sungrow SG3.6RS")
    assert coordinator._modbus_client is None
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_modbus_only_update_keeps_last_good_within_grace(hass: HomeAssistant):
    """A transient Modbus blip keeps serving the last-good data (availability grace).

    The Modbus client wraps raw pymodbus/OSError failures into ``SungrowModbusError``
    before they reach the coordinator, so the test injects the wrapped type.
    """
    from custom_components.sungrow.modbus import SungrowModbusError

    coordinator = _modbus_only_coordinator(hass)
    coordinator.data = {"grid_frequency": {"value": 50.0}}
    coordinator._last_successful_update = hass.loop.time()  # fresh success
    coordinator._modbus_client.async_read_realtime = AsyncMock(side_effect=SungrowModbusError("connection reset"))

    data = await coordinator._async_update_data()

    assert data == {"grid_frequency": {"value": 50.0}}


async def test_modbus_only_update_raises_outside_grace(hass: HomeAssistant):
    """Once the last-good data is stale, a Modbus failure takes the entry unavailable."""
    from custom_components.sungrow.modbus import SungrowModbusError

    coordinator = _modbus_only_coordinator(hass)
    coordinator.data = {"grid_frequency": {"value": 50.0}}
    coordinator._last_successful_update = None  # no recent success -> outside grace
    coordinator._modbus_client.async_read_realtime = AsyncMock(side_effect=SungrowModbusError("connection reset"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# #223 daily_yield diagnostic capture
# ---------------------------------------------------------------------------


async def test_modbus_only_keeps_previous_diagnostic_on_capture_failure(hass: HomeAssistant):
    """A Modbus diagnostic-read failure keeps the previous diagnostic in place so a
    transient blip never clears evidence the user is collecting for #223.
    """
    coordinator = _modbus_only_coordinator(hass)
    previous = {
        "start": 4999,
        "raw": {"5002": 640},
        "candidates": [],
        "current_mapping": {"address": 5002, "raw": 640, "scale": 0.1, "unit": "kWh"},
    }
    coordinator.daily_yield_diagnostic = previous
    coordinator.config_entry.options = {CONF_MODBUS_DEBUG_DAILY_YIELD: True}
    from custom_components.sungrow.modbus import SungrowModbusError

    coordinator._modbus_client.async_read_realtime = AsyncMock(
        return_value={"daily_yield": {"code": "daily_yield", "value": 64.0, "unit": "kWh", "source": "modbus"}}
    )
    coordinator._modbus_client.async_read_daily_yield_diagnostic = AsyncMock(side_effect=SungrowModbusError("boom"))
    await coordinator._async_modbus_only_update()
    assert coordinator.daily_yield_diagnostic is previous


async def test_modbus_only_update_captures_daily_yield_diagnostic(hass: HomeAssistant):
    """The Modbus-only path refreshes the diagnostic when the debug option is on."""
    coordinator = _modbus_only_coordinator(hass)
    coordinator.config_entry.options = {CONF_MODBUS_DEBUG_DAILY_YIELD: True}
    coordinator._modbus_client.async_read_realtime = AsyncMock(
        return_value={"daily_yield": {"code": "daily_yield", "value": 64.0, "unit": "kWh", "source": "modbus"}}
    )
    coordinator._modbus_client.async_read_daily_yield_diagnostic = AsyncMock(
        return_value={
            "start": 4999,
            "raw": {"5002": 640},
            "current_mapping": {"address": 5002, "raw": 640, "scale": 0.1, "unit": "kWh"},
        }
    )
    await coordinator._async_update_data()
    assert coordinator.daily_yield_diagnostic is not None
    assert coordinator.daily_yield_diagnostic["raw"]["5002"] == 640


async def test_capture_daily_yield_diagnostic_noop_without_client(hass: HomeAssistant):
    """Cloud-only entries never call the diagnostic helper (#223 is a Modbus-only concern)."""
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), MagicMock(), "12345", "Test Plant")
    assert coordinator._modbus_client is None
    await coordinator._async_capture_daily_yield_diagnostic()
    assert coordinator.daily_yield_diagnostic is None


# ---------------------------------------------------------------------------
# Bug condition exploration (hybrid MPPT / string sensors) — Property 1
#
# These tests encode the EXPECTED (fixed) behavior for SH-family hybrids reported
# as DeviceType.ENERGY_STORAGE_SYSTEM. They MUST FAIL on the unfixed code: the ESS
# branch currently reuses INVERTER_DIAGNOSTIC_POINTS, which requests the string-
# inverter MPPT IDs ("5"-"10") and never the hybrid 13xxx MPPT IDs. Failure here
# confirms the bug exists (root cause #1). Validates: Requirements 2.1
# ---------------------------------------------------------------------------

# The hybrid MPPT point IDs an ESS/hybrid must request (MPPT1-4 voltage/current).
HYBRID_MPPT_POINT_IDS = {
    "13001",
    "13002",
    "13105",
    "13106",
    "13107",
    "13108",
    "13109",
    "13110",
}
# The string-inverter MPPT IDs that must NOT be requested for an ESS (they share the
# mpptN_* codes with the 13xxx IDs, so requesting both collides in the per-device merge).
STRING_INVERTER_MPPT_POINT_IDS = {"5", "6", "7", "8", "9", "10"}


async def test_ess_requests_hybrid_mppt_points(hass: HomeAssistant):
    """Bug condition (Property 1): an ESS with device sensors ON requests the hybrid MPPT IDs.

    Mirrors test_ess_operating_status_avoids_point29_collision. On the UNFIXED code the ESS
    branch reuses INVERTER_DIAGNOSTIC_POINTS and never requests the 13xxx MPPT IDs, so this
    subset assertion FAILS — which is the intended proof that the bug exists.

    Validates: Requirements 2.1
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "ps_key": "12345_14_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    # Assertion A: the eight hybrid MPPT IDs are requested for the ESS device.
    missing = HYBRID_MPPT_POINT_IDS - set(extra)
    assert not missing, f"ESS extra_measure_points is missing hybrid MPPT IDs: {sorted(missing)}"


async def test_ess_drops_string_inverter_mppt_points(hass: HomeAssistant):
    """Bug condition (Property 1 / Property 2 final clause): ESS omits the string-inverter MPPT IDs.

    Because "5"-"10" and the 13xxx IDs both map to the mpptN_* codes, requesting both for a single
    ESS device would silently overwrite one another in the per-device merge. On the UNFIXED code
    "5"-"10" are present, so this assertion FAILS — the collision source is confirmed.

    Validates: Requirements 2.1
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "ps_key": "12345_14_1_1"}]
    coordinator = SungrowPlantCoordinator(
        hass, _make_entry({CONF_ENABLE_DEVICE_SENSORS: True}), plants, "12345", "Test Plant", devices
    )

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    # Assertion B: the string-inverter MPPT IDs are absent from the ESS extra.
    present = STRING_INVERTER_MPPT_POINT_IDS & set(extra)
    assert not present, f"ESS extra_measure_points still contains string-inverter MPPT IDs: {sorted(present)}"


# ---------------------------------------------------------------------------
# Preservation (hybrid MPPT / string sensors) — Property 2
#
# These tests capture the BASELINE behavior of every non-buggy device/config path
# so the upcoming fix can be proven to leave it byte-for-byte identical. They are
# written observation-first and MUST PASS on the unfixed code. The ESS-with-sensors-ON
# path is the buggy combination and is therefore NOT asserted for full equality here;
# only its operating-status handling (13146 requested, 29 dropped) is preserved.
# Validates: Requirements 3.1, 3.2, 3.3, 3.4
# ---------------------------------------------------------------------------

# Sentinel: the coordinator skips the per-device fetch entirely (no diagnostic set to
# request), so async_get_device_realtime is never awaited for that combination.
_DEVICE_REALTIME_NOT_CALLED = object()

# The string-inverter per-string DC voltage/current IDs that INVERTER must keep requesting.
_STRING_INVERTER_STRING_POINT_IDS = {
    "96",
    "70",
    "97",
    "71",
    "98",
    "72",
    "99",
    "73",
    "100",
    "74",
    "101",
    "75",
    "102",
    "76",
    "103",
    "77",
}


async def _capture_device_extra(hass, device_type, *, enable_device_sensors):
    """Run a single-device update and return the captured extra_measure_points.

    Mirrors test_ess_operating_status_avoids_point29_collision: a MagicMock plants
    service captures the extra_measure_points kwarg passed to async_get_device_realtime.
    Returns _DEVICE_REALTIME_NOT_CALLED when the coordinator skips the per-device fetch.
    """
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "dev-1", "device_type": device_type, "ps_key": "12345_1_1_1"}]
    options = {CONF_ENABLE_DEVICE_SENSORS: True} if enable_device_sensors else {}
    coordinator = SungrowPlantCoordinator(hass, _make_entry(options), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    if plants.async_get_device_realtime.await_args is None:
        return _DEVICE_REALTIME_NOT_CALLED
    return plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]


def _golden_preservation_cases():
    """Golden (device_type, enable_device_sensors) -> expected extra for every non-buggy path.

    Composed from the const point maps exactly as the coordinator selects them, which is the
    unchanged baseline for these paths. ESS + sensors ON is intentionally excluded — it is the
    buggy combination the fix will change and is covered by dedicated preservation assertions.
    """
    return [
        # String inverter: full diagnostic set (MPPT "5"-"10" + per-string + grid health).
        pytest.param(DeviceType.INVERTER, True, dict(INVERTER_DIAGNOSTIC_POINTS), id="inverter-on"),
        # Sensors off: only the single operating-status point, no heavy diagnostic set.
        pytest.param(DeviceType.INVERTER, False, dict(INVERTER_OPERATING_STATUS_POINT), id="inverter-off"),
        pytest.param(
            DeviceType.ENERGY_STORAGE_SYSTEM,
            False,
            {**ESS_OPERATING_STATUS_POINT, **ESS_BATTERY_POWER_POINTS},
            id="ess-off",
        ),
        # Battery / meter / comm each request their existing point set unchanged.
        pytest.param(DeviceType.BATTERY, True, dict(BATTERY_DEVICE_POINTS), id="battery-on"),
        pytest.param(DeviceType.METER, True, dict(METER_DEVICE_POINTS), id="meter-on"),
        pytest.param(DeviceType.COMMUNICATION_MODULE, True, dict(COMM_MODULE_POINTS), id="comm-on"),
        # Unmapped type with sensors on: no diagnostic set -> extra collapses to None.
        pytest.param(999, True, None, id="unmapped-on"),
        # Sensors off + no operating-status point -> the per-device fetch is skipped entirely.
        pytest.param(DeviceType.BATTERY, False, _DEVICE_REALTIME_NOT_CALLED, id="battery-off"),
        pytest.param(DeviceType.METER, False, _DEVICE_REALTIME_NOT_CALLED, id="meter-off"),
        pytest.param(DeviceType.COMMUNICATION_MODULE, False, _DEVICE_REALTIME_NOT_CALLED, id="comm-off"),
        pytest.param(999, False, _DEVICE_REALTIME_NOT_CALLED, id="unmapped-off"),
    ]


@pytest.mark.parametrize(("device_type", "enable_device_sensors", "expected"), _golden_preservation_cases())
async def test_non_buggy_device_config_extra_measure_points_preserved(
    hass: HomeAssistant, device_type, enable_device_sensors, expected
):
    """Property 2: every non-buggy device/config requests exactly its golden point set.

    Generated over device type x enable_device_sensors; for each combination the captured
    extra_measure_points must equal the observed baseline. Passing on the unfixed code fixes
    the behavior the upcoming fix must preserve byte-for-byte.

    Validates: Requirements 3.1, 3.3, 3.4
    """
    captured = await _capture_device_extra(hass, device_type, enable_device_sensors=enable_device_sensors)

    assert captured == expected


async def test_inverter_mppt_and_string_points_preserved(hass: HomeAssistant):
    """Property 2: INVERTER + sensors on keeps its MPPT ("5"-"10") and per-string IDs.

    Validates: Requirement 3.1
    """
    extra = await _capture_device_extra(hass, DeviceType.INVERTER, enable_device_sensors=True)

    assert set(extra) >= STRING_INVERTER_MPPT_POINT_IDS  # "5"-"10" still requested
    assert set(extra) >= _STRING_INVERTER_STRING_POINT_IDS  # "96"/"70" .. "103"/"77" still requested


async def test_ess_operating_status_handling_preserved(hass: HomeAssistant):
    """Property 2: ESS + sensors on still requests 13146 for operating status and drops 29.

    This is the one aspect of the (otherwise buggy) ESS-with-sensors-on path that the fix
    must preserve exactly: the 13146/29 operating-status collision handling (#182).

    Validates: Requirement 3.2
    """
    extra = await _capture_device_extra(hass, DeviceType.ENERGY_STORAGE_SYSTEM, enable_device_sensors=True)

    assert extra["13146"] == "operating_status"
    assert "29" not in extra


# ---------------------------------------------------------------------------
# Fix Checking / Preservation Checking (hybrid MPPT / string sensors)
#
# FOR ALL X WHERE isBugCondition(X): the fixed ESS branch requests the 13xxx IDs and
# drops "5"-"10" (covered above by test_ess_requests_hybrid_mppt_points /
# test_ess_drops_string_inverter_mppt_points, which now PASS on the fixed code).
#
# Preservation Checking (Property 2 final clause): for an ESS device the requested IDs
# must never contain both a "5"-"10" ID and its 13xxx counterpart, so no two point IDs
# map to the same mpptN_* code and silently overwrite each other in the per-device merge.
# Validates: Requirements 2.1, 3.1, 3.2
# ---------------------------------------------------------------------------


async def test_ess_mppt_ids_have_no_code_collision(hass: HomeAssistant):
    """Property 2: no two requested ESS IDs map to the same mpptN_* code.

    Because "5" and "13001" both resolve to mppt1_voltage (and so on), requesting both
    for one ESS device would let one silently overwrite the other in the per-device merge.
    The fix swaps "5"-"10" for the 13xxx IDs, so each mpptN_* code is requested exactly once.

    Validates: Requirements 2.1, 3.1, 3.2
    """
    extra = await _capture_device_extra(hass, DeviceType.ENERGY_STORAGE_SYSTEM, enable_device_sensors=True)

    # Group the requested point IDs by the mpptN_* code they resolve to.
    ids_by_mppt_code: dict[str, list[str]] = {}
    for pid, code in extra.items():
        if code.startswith("mppt"):
            ids_by_mppt_code.setdefault(code, []).append(pid)

    collisions = {code: pids for code, pids in ids_by_mppt_code.items() if len(pids) > 1}
    assert not collisions, f"mpptN_* codes requested via more than one point ID for ESS: {collisions}"

    # And concretely: the string-inverter and hybrid MPPT ID ranges are disjoint in the request.
    requested = set(extra)
    assert not (STRING_INVERTER_MPPT_POINT_IDS & requested & HYBRID_MPPT_POINT_IDS)
    assert not (STRING_INVERTER_MPPT_POINT_IDS & requested)  # "5"-"10" fully dropped for ESS


# ---------------------------------------------------------------------------
# Cloud user-account transport (#268)
# ---------------------------------------------------------------------------


async def test_user_update_maps_plant_detail_points(hass: HomeAssistant):
    """A cloud_user coordinator maps getPsDetail onto measure points (#269)."""
    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(
        return_value={
            "curr_power": {"value": "0.49", "unit": "kW"},
            "p83106_map_virgin": {"value": "12500", "unit": "Wh"},
        }
    )
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), None, "12345", "Test Plant", user_auth=client)

    data = await coordinator._async_update_data()

    client.async_get_plant_detail.assert_awaited_once_with("12345")
    # kW is normalised to W for the cloud_user path (×1000).
    assert data["current_power"]["value"] == 490.0
    assert data["current_power"]["unit"] == "W"
    assert data["current_power"]["source"] == "cloud_user"
    # The 83106 measure point is present (Wh normalised to kWh downstream).
    assert "83106" in data


def _user_battery_device():
    """A device-list entry shaped like the #389 report (SBR096 battery, SOC 58604)."""
    return {
        "uuid": "dev-battery-1",
        "device_type": 43,
        "device_name": "SBR096",
        "point_data": [
            {"point_id": 58604, "point_name": "Battery SOC", "unit": "%", "value": "32.2"},
        ],
    }


async def test_user_update_populates_device_data_from_device_list(hass: HomeAssistant):
    """Per-device points come from the user-API device list's embedded point_data (#389).

    Before this, ``_async_user_update`` returned before the OAuth path's per-device
    fetch, so ``device_data`` stayed empty and the sensor platform's per-device loop
    never ran — Battery SOC was returned by the API but produced no entity.
    """
    from custom_components.sungrow.const import CONF_ENABLE_DEVICE_SENSORS

    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(return_value={"curr_power": {"value": "0.49", "unit": "kW"}})
    client.async_get_devices = AsyncMock(return_value=[_user_battery_device()])
    entry = _make_entry(options={CONF_ENABLE_DEVICE_SENSORS: True})
    coordinator = SungrowPlantCoordinator(hass, entry, None, "12345", "Test Plant", user_auth=client)

    await coordinator._async_update_data()

    client.async_get_devices.assert_awaited_once_with("12345")
    assert coordinator.device_data["dev-battery-1"]["58604"]["value"] == "32.2"
    assert coordinator.device_data["dev-battery-1"]["58604"]["source"] == "cloud_user"


async def test_user_update_skips_device_fetch_when_option_off(hass: HomeAssistant):
    """Nothing else consumes device_data on this transport, so the call isn't spent."""
    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(return_value={"curr_power": {"value": "1", "unit": "W"}})
    client.async_get_devices = AsyncMock(return_value=[_user_battery_device()])
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), None, "12345", "Test Plant", user_auth=client)

    await coordinator._async_update_data()

    client.async_get_devices.assert_not_awaited()
    assert coordinator.device_data == {}


async def test_user_update_device_fetch_failure_is_non_fatal(hass: HomeAssistant):
    """A failed device fetch must not fail the poll; plant points still update."""
    from custom_components.sungrow.const import CONF_ENABLE_DEVICE_SENSORS

    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(return_value={"curr_power": {"value": "0.49", "unit": "kW"}})
    client.async_get_devices = AsyncMock(side_effect=PySolarCloudException({"error": "rate_limit"}))
    entry = _make_entry(options={CONF_ENABLE_DEVICE_SENSORS: True})
    coordinator = SungrowPlantCoordinator(hass, entry, None, "12345", "Test Plant", user_auth=client)

    data = await coordinator._async_update_data()

    assert data["current_power"]["value"] == 490.0
    assert coordinator.device_data == {}


async def test_user_update_refreshes_the_live_device_list(hass: HomeAssistant):
    """A device that appears after setup is picked up, so its sensors arrive at runtime.

    The entity builders read ``coordinator.devices`` on every poll, so refreshing it
    here is what lets a newly-added battery surface without a reload.
    """
    from custom_components.sungrow.const import CONF_ENABLE_DEVICE_SENSORS

    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(return_value={"curr_power": {"value": "1", "unit": "W"}})
    client.async_get_devices = AsyncMock(return_value=[_user_battery_device()])
    entry = _make_entry(options={CONF_ENABLE_DEVICE_SENSORS: True})
    coordinator = SungrowPlantCoordinator(
        hass, entry, None, "12345", "Test Plant", [{"uuid": "stale", "device_type": 1}], user_auth=client
    )

    await coordinator._async_update_data()

    assert [d["uuid"] for d in coordinator.devices] == ["dev-battery-1"]


async def test_user_update_keeps_devices_when_refresh_returns_empty(hass: HomeAssistant):
    """An empty device list is treated as a blip, not as "all devices removed"."""
    from custom_components.sungrow.const import CONF_ENABLE_DEVICE_SENSORS

    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(return_value={"curr_power": {"value": "1", "unit": "W"}})
    client.async_get_devices = AsyncMock(return_value=[])
    entry = _make_entry(options={CONF_ENABLE_DEVICE_SENSORS: True})
    known = [_user_battery_device()]
    coordinator = SungrowPlantCoordinator(hass, entry, None, "12345", "Test Plant", known, user_auth=client)

    await coordinator._async_update_data()

    assert [d["uuid"] for d in coordinator.devices] == ["dev-battery-1"]
    # Points still mapped from the retained list.
    assert coordinator.device_data["dev-battery-1"]["58604"]["value"] == "32.2"


async def test_user_update_auth_error_triggers_reauth(hass: HomeAssistant):
    """A dead user-account credential raises ConfigEntryAuthFailed so HA starts reauth (#268)."""
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from pysolarcloud import AuthError

    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(side_effect=AuthError({"error": "user_login_failed"}))
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), None, "12345", "Test Plant", user_auth=client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_user_update_transient_rides_grace_window(hass: HomeAssistant):
    """A transient user-account failure keeps last-good data within the grace window (#268/#152)."""
    client = MagicMock()
    client.async_get_plant_detail = AsyncMock(side_effect=ClientError("network blip"))
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), None, "12345", "Test Plant", user_auth=client)
    coordinator.data = {"total_active_power": {"value": "1.2"}}
    coordinator._last_successful_update = hass.loop.time()  # recent success

    data = await coordinator._async_update_data()

    assert data == {"total_active_power": {"value": "1.2"}}
