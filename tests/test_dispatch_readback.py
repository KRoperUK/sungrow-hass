"""Tests for the periodic EMS dispatch read-back (#286)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from custom_components.sungrow.select import (
    BATTERY_MODE_FORCE_CHARGE,
    BATTERY_MODE_SELF_CONSUMPTION,
    DISPATCH_SELECTS,
    SungrowDispatchSelect,
)


def _make_select(hass: HomeAssistant, *, param: str = "battery_mode") -> SungrowDispatchSelect:
    coordinator = MagicMock()
    coordinator.plant_id = "plant_1"
    coordinator.plant_name = "Test Plant"
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "entry_1"
    coordinator.has_battery = True
    coordinator.dispatch_update_supported = True
    coordinator.forced_dispatch_duration_minutes = 0
    coordinator.devices = [{"uuid": "dev-1", "device_type": MagicMock(value=14)}]

    control = MagicMock(spec=Control)
    control.async_update_parameters = AsyncMock(return_value=[])
    control.async_read_parameters = AsyncMock(return_value=[])

    device = {"uuid": "dev-1", "device_name": "Inverter", "device_type": MagicMock(value=14)}
    meta = DISPATCH_SELECTS[param]
    select = SungrowDispatchSelect(coordinator, control, device, param, meta)
    select.hass = hass
    select._removed = False
    return select


def _trigger_readback(select: SungrowDispatchSelect) -> None:
    """Fire 3 coordinator updates to trigger the throttled read-back."""
    with patch.object(select, "async_write_ha_state"):
        select._handle_coordinator_update()
        select._handle_coordinator_update()
        select._handle_coordinator_update()


async def test_readback_not_triggered_before_third_poll(hass: HomeAssistant):
    """Read-back only fires every 3rd coordinator update."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE

    with patch.object(select, "async_write_ha_state"):
        select._handle_coordinator_update()
        select._handle_coordinator_update()
    await hass.async_block_till_done()

    select.control.async_read_parameters.assert_not_awaited()


async def test_readback_fires_on_third_poll(hass: HomeAssistant):
    """Read-back fires on the 3rd coordinator update."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select.control.async_read_parameters = AsyncMock(
        return_value=[{"id": "10003", "code": "energy_management_mode", "value": 2}]
    )

    _trigger_readback(select)
    await hass.async_block_till_done()

    select.control.async_read_parameters.assert_awaited_once()
    assert select._attr_current_option == BATTERY_MODE_FORCE_CHARGE


async def test_readback_detects_external_revert_to_self_consumption(hass: HomeAssistant):
    """If read-back shows Self-consumption while we assume forced, sync the state."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select.control.async_read_parameters = AsyncMock(
        return_value=[{"id": "10003", "code": "energy_management_mode", "value": 0}]
    )

    with patch("custom_components.sungrow.select.async_stop_heartbeat", new_callable=AsyncMock) as stop_mock:
        _trigger_readback(select)
        await hass.async_block_till_done()

    assert select._attr_current_option == BATTERY_MODE_SELF_CONSUMPTION
    stop_mock.assert_awaited_once()


async def test_readback_no_action_when_safe_and_self_consumption(hass: HomeAssistant):
    """If entity is already Self-consumption and read-back confirms, no change."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_SELF_CONSUMPTION
    select.control.async_read_parameters = AsyncMock(
        return_value=[{"id": "10003", "code": "energy_management_mode", "value": 0}]
    )

    _trigger_readback(select)
    await hass.async_block_till_done()

    assert select._attr_current_option == BATTERY_MODE_SELF_CONSUMPTION


async def test_readback_unknown_does_not_change_state(hass: HomeAssistant):
    """If read-back returns unknown (read error), state stays unchanged."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select.control.async_read_parameters = AsyncMock(side_effect=PySolarCloudException("network"))

    _trigger_readback(select)
    await hass.async_block_till_done()

    assert select._attr_current_option == BATTERY_MODE_FORCE_CHARGE


async def test_readback_skipped_for_non_battery_mode_selects(hass: HomeAssistant):
    """Non-battery-mode selects never trigger read-back."""
    select = _make_select(hass, param="feed_in_limitation")

    with patch.object(select, "async_write_ha_state"):
        for _ in range(10):
            select._handle_coordinator_update()
    await hass.async_block_till_done()

    select.control.async_read_parameters.assert_not_awaited()


async def test_readback_counter_resets_after_firing(hass: HomeAssistant):
    """The counter resets after the read-back fires, so it fires again after 3 more."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select.control.async_read_parameters = AsyncMock(
        return_value=[{"id": "10003", "code": "energy_management_mode", "value": 2}]
    )

    # First batch: fires on 3rd.
    _trigger_readback(select)
    await hass.async_block_till_done()
    assert select.control.async_read_parameters.await_count == 1

    # Second batch: fires again on 6th total (3rd since reset).
    _trigger_readback(select)
    await hass.async_block_till_done()
    assert select.control.async_read_parameters.await_count == 2


async def test_readback_skipped_when_removed(hass: HomeAssistant):
    """If the entity was removed, read-back is a no-op."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select._removed = True

    _trigger_readback(select)
    await hass.async_block_till_done()

    select.control.async_read_parameters.assert_not_awaited()
