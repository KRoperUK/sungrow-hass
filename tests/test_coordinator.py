"""Tests for the Sungrow data update coordinator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pysolarcloud import AuthError, PySolarCloudException
from pysolarcloud.plants import DeviceType

from custom_components.sungrow.const import (
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_SCAN_INTERVAL,
    DEVICE_REFRESH_INTERVAL,
)
from custom_components.sungrow.coordinator import SungrowPlantCoordinator, describe_api_error, is_auth_error

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
    """With the option off, per-device realtime is never requested."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
    plants.async_get_device_realtime = AsyncMock(return_value={})
    devices = [{"uuid": "chg-1", "device_type": 999}]
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant", devices)

    await coordinator._async_update_data()

    assert coordinator.device_data == {}
    plants.async_get_device_realtime.assert_not_awaited()


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
