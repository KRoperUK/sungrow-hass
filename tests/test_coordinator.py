"""Tests for the Sungrow data update coordinator."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pysolarcloud import PySolarCloudException

from custom_components.sungrow.const import CONF_EXTRA_MEASURE_POINTS, CONF_SCAN_INTERVAL
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
    """Default scan interval is applied when no option is set."""
    coordinator = SungrowPlantCoordinator(hass, _make_entry(), MagicMock(), "1", "P")
    assert coordinator.update_interval.total_seconds() == 5 * 60


async def test_scan_interval_from_options(hass: HomeAssistant):
    """Scan interval option overrides the default."""
    entry = _make_entry({CONF_SCAN_INTERVAL: 30})
    coordinator = SungrowPlantCoordinator(hass, entry, MagicMock(), "1", "P")
    assert coordinator.update_interval.total_seconds() == 30 * 60


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
