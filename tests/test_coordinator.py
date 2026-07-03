"""Tests for the Sungrow data update coordinator."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pysolarcloud import PySolarCloudException
from pysolarcloud.plants import DeviceType

from custom_components.sungrow.const import (
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_SCAN_INTERVAL,
)
from custom_components.sungrow.coordinator import SungrowPlantCoordinator, is_auth_error

from .conftest import MOCK_REALTIME_DATA


def _make_entry(options=None):
    entry = MagicMock()
    entry.options = options or {}
    return entry


# ---------------------------------------------------------------------------
# is_auth_error
# ---------------------------------------------------------------------------


def test_is_auth_error_keyerror():
    """A KeyError for access_token (failed refresh) is treated as an auth error."""
    assert is_auth_error(KeyError("access_token")) is True


def test_is_auth_error_unrelated_keyerror():
    """An unrelated KeyError is not treated as an auth error."""
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
    plants.async_get_realtime_data = AsyncMock(side_effect=KeyError("access_token"))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
    with pytest.raises(ConfigEntryAuthFailed):
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


async def test_update_data_unrelated_keyerror_raises_update_failed(hass: HomeAssistant):
    """A KeyError unrelated to tokens is treated as a transient UpdateFailed."""
    plants = MagicMock()
    plants.async_get_realtime_data = AsyncMock(side_effect=KeyError("some_other_key"))

    coordinator = SungrowPlantCoordinator(hass, _make_entry(), plants, "12345", "Test Plant")
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
