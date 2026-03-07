"""Tests for Sungrow component setup."""

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import async_setup
from custom_components.sungrow.const import DOMAIN

from .conftest import MOCK_CONFIG_DATA


# ---------------------------------------------------------------------------
# async_setup
# ---------------------------------------------------------------------------


async def test_async_setup(hass: HomeAssistant):
    """Test async_setup returns True."""
    result = await async_setup(hass, {})
    assert result is True


# ---------------------------------------------------------------------------
# async_setup_entry / async_unload_entry
# ---------------------------------------------------------------------------


async def test_async_setup_entry(hass: HomeAssistant, mock_sensor_api):
    """Test a successful setup entry stores data and forwards platforms."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sungrow.sensor.async_setup_entry",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]


async def test_async_unload_entry(hass: HomeAssistant, mock_sensor_api):
    """Test successful unload removes data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sungrow.sensor.async_setup_entry",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_async_setup_entry_stores_data(hass: HomeAssistant, mock_sensor_api):
    """Test that setup stores entry data under hass.data[DOMAIN][entry_id]."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sungrow.sensor.async_setup_entry",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    stored = hass.data[DOMAIN][entry.entry_id]
    assert stored == MOCK_CONFIG_DATA
