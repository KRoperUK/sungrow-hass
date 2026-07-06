"""Tests for the Sungrow data update coordinator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pysolarcloud import AuthError, PySolarCloudException
from pysolarcloud.plants import DeviceType

from custom_components.sungrow.const import (
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_SCAN_INTERVAL,
    DEVICE_REFRESH_INTERVAL,
    DOMAIN,
)
from custom_components.sungrow.coordinator import (
    BACKOFF_MAX_INTERVAL,
    SungrowPlantCoordinator,
    describe_api_error,
    is_auth_error,
)

from .conftest import MOCK_REALTIME_DATA


def _make_entry(options=None):
    entry = MagicMock()
    entry.options = options or {}
    return entry


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


async def test_refresh_devices_updates_list(hass: HomeAssistant):
    """Each poll refreshes the plant's device list so new devices are picked up."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})
    plants.async_get_plant_devices = AsyncMock(return_value=[{"uuid": "new-dev"}])

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices=[])
    await coordinator._async_update_data()

    assert coordinator.devices == [{"uuid": "new-dev"}]


async def test_refresh_devices_failure_keeps_previous_list(hass: HomeAssistant):
    """A device-list fetch failure keeps the previous list (best effort, non-fatal)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})
    plants.async_get_plant_devices = AsyncMock(side_effect=ConnectionError("boom"))

    coordinator = SungrowPlantCoordinator(
        hass, _make_entry(), plants, "12345", "Test Plant", devices=[{"uuid": "keep"}]
    )
    await coordinator._async_update_data()

    assert coordinator.devices == [{"uuid": "keep"}]


async def test_device_list_refresh_is_throttled(hass: HomeAssistant):
    """The device list is refreshed on the first poll, then only once the interval elapses (#115)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value={"12345": {}})
    plants.async_get_plant_devices = AsyncMock(return_value=[{"uuid": "dev"}])

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
    """ESS/hybrid operating status is requested on point 13146, not the inverter point (#182)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "ps_key": "12345_14_1_1"}]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    extra = plants.async_get_device_realtime.await_args.kwargs["extra_measure_points"]
    assert extra == {"13146": "operating_status"}


async def test_device_data_best_effort_on_error(hass: HomeAssistant):
    """A failing device type is skipped without failing the whole update."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)

    async def _realtime(plant_id, device_type, **kwargs):
        if getattr(device_type, "value", device_type) == 999:
            raise RuntimeError("charger endpoint down")
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
    assert "29" in extra  # inverter operating status
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


async def test_plant_detail_fetched_and_stored(hass: HomeAssistant):
    """The plant-detail fields are fetched during a poll and stored on the coordinator (#178)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_plant_details = AsyncMock(
        return_value=[{"alarm_count": 0, "fault_count": 1, "install_power": 3600.0, "power_price_unit": "GBP"}]
    )
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", [])

    await coordinator._async_update_data()

    assert coordinator.plant_detail["install_power"] == 3600.0
    assert coordinator.plant_detail["fault_count"] == 1
    plants.async_get_plant_details.assert_awaited()


async def test_plant_detail_best_effort_on_error(hass: HomeAssistant):
    """A failing plant-detail fetch doesn't fail the whole poll (#178)."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_plant_details = AsyncMock(side_effect=RuntimeError("detail endpoint down"))
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", [])

    await coordinator._async_update_data()  # must not raise

    assert coordinator.plant_detail == {}
