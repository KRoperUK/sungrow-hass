"""Tests for select.py actuation verification and heartbeat lifecycle (#290)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from custom_components.sungrow.select import (
    BATTERY_MODE_FORCE_CHARGE,
    BATTERY_MODE_FORCE_DISCHARGE,
    BATTERY_MODE_SELF_CONSUMPTION,
    DISPATCH_SELECTS,
    SungrowDispatchSelect,
)


def _make_coordinator(hass: HomeAssistant) -> MagicMock:
    coordinator = MagicMock()
    coordinator.plant_id = "plant_1"
    coordinator.plant_name = "Test Plant"
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "entry_1"
    coordinator.has_battery = True
    coordinator.dispatch_update_supported = True
    coordinator.forced_dispatch_duration_minutes = 0
    coordinator.devices = [{"uuid": "dev-1", "device_type": MagicMock(value=14)}]
    return coordinator


def _make_select(hass: HomeAssistant, *, param: str = "battery_mode") -> SungrowDispatchSelect:
    coordinator = _make_coordinator(hass)
    control = MagicMock(spec=Control)
    control.async_update_parameters = AsyncMock(return_value=[])
    control.async_read_parameters = AsyncMock(return_value=[])
    device = {"uuid": "dev-1", "device_name": "Inverter", "device_type": MagicMock(value=14)}
    meta = DISPATCH_SELECTS[param]
    select = SungrowDispatchSelect(coordinator, control, device, param, meta)
    select.hass = hass
    select._removed = False
    return select


# ---------------------------------------------------------------------------
# _reads_as_self_consumption
# ---------------------------------------------------------------------------


class TestReadsAsSelfConsumption:
    """Tests for the static _reads_as_self_consumption helper."""

    def test_numeric_zero_means_self_consumption(self):
        """EMS mode value 0 = Self-consumption → True."""
        params = [{"id": "10003", "code": "energy_management_mode", "value": 0}]
        assert SungrowDispatchSelect._reads_as_self_consumption(params) is True

    def test_numeric_nonzero_means_actuated(self):
        """EMS mode value 2 (compulsory) = actuated → False."""
        params = [{"id": "10003", "code": "energy_management_mode", "value": 2}]
        assert SungrowDispatchSelect._reads_as_self_consumption(params) is False

    def test_string_self_consumption_means_true(self):
        """Text containing 'self' reads as Self-consumption → True."""
        params = [{"id": "10003", "code": "energy_management_mode", "value": "Self-consumption"}]
        assert SungrowDispatchSelect._reads_as_self_consumption(params) is True

    def test_string_forced_means_false(self):
        """Text 'compulsory' means actuated → False."""
        params = [{"id": "10003", "code": "energy_management_mode", "value": "compulsory"}]
        assert SungrowDispatchSelect._reads_as_self_consumption(params) is False

    def test_none_value_means_unknown(self):
        """None value → unknown."""
        params = [{"id": "10003", "code": "energy_management_mode", "value": None}]
        assert SungrowDispatchSelect._reads_as_self_consumption(params) is None

    def test_empty_params_means_unknown(self):
        """No EMS-mode param in the list → unknown."""
        assert SungrowDispatchSelect._reads_as_self_consumption([]) is None

    def test_empty_string_means_unknown(self):
        """Empty string → unknown."""
        params = [{"id": "10003", "code": "energy_management_mode", "value": ""}]
        assert SungrowDispatchSelect._reads_as_self_consumption(params) is None


# ---------------------------------------------------------------------------
# _verify_actuation
# ---------------------------------------------------------------------------


async def test_verify_actuation_success_clears_issue(hass: HomeAssistant):
    """If read-back shows NOT self-consumption, any prior Repair is cleared."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    # Read-back says "compulsory" (value=2) → actuated → clear issue.
    select.control.async_read_parameters = AsyncMock(
        return_value=[{"id": "10003", "code": "energy_management_mode", "value": 2}]
    )

    with patch.object(select, "_clear_not_actuated_issue") as clear_mock:
        await select._verify_actuation()

    clear_mock.assert_called_once()


async def test_verify_actuation_retries_on_self_consumption(hass: HomeAssistant):
    """If read-back says Self-consumption, retries the write once."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    # First read: still self-consumption; after retry: still self-consumption → Repair.
    select.control.async_read_parameters = AsyncMock(
        return_value=[{"id": "10003", "code": "energy_management_mode", "value": 0}]
    )

    with patch.object(select, "_raise_not_actuated_issue") as raise_mock:
        await select._verify_actuation()

    # The write was retried.
    assert select.control.async_update_parameters.await_count == 1
    raise_mock.assert_called_once()


async def test_verify_actuation_retry_succeeds(hass: HomeAssistant):
    """If retry write makes the inverter leave Self-consumption, issue is cleared."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_DISCHARGE
    # First read: self-consumption (0); after retry: compulsory (2).
    select.control.async_read_parameters = AsyncMock(
        side_effect=[
            [{"id": "10003", "code": "energy_management_mode", "value": 0}],
            [{"id": "10003", "code": "energy_management_mode", "value": 2}],
        ]
    )

    with patch.object(select, "_clear_not_actuated_issue") as clear_mock:
        await select._verify_actuation()

    clear_mock.assert_called_once()


async def test_verify_actuation_unknown_readback_no_action(hass: HomeAssistant):
    """If read-back is unknown (None), no issue is raised or cleared."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    # Read-back returns empty list → unknown.
    select.control.async_read_parameters = AsyncMock(return_value=[])

    with (
        patch.object(select, "_raise_not_actuated_issue") as raise_mock,
        patch.object(select, "_clear_not_actuated_issue") as clear_mock,
    ):
        await select._verify_actuation()

    raise_mock.assert_not_called()
    clear_mock.assert_not_called()


async def test_verify_actuation_skipped_when_removed(hass: HomeAssistant):
    """If the entity was removed, verification is a no-op."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select._removed = True

    await select._verify_actuation()

    select.control.async_read_parameters.assert_not_awaited()


async def test_verify_actuation_skipped_for_safe_mode(hass: HomeAssistant):
    """If current option is a safe mode, verification is a no-op."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_SELF_CONSUMPTION

    await select._verify_actuation()

    select.control.async_read_parameters.assert_not_awaited()


async def test_verify_actuation_read_error_skips_gracefully(hass: HomeAssistant):
    """A read-back error results in None (unknown), no issue raised."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select.control.async_read_parameters = AsyncMock(side_effect=PySolarCloudException("network"))

    with patch.object(select, "_raise_not_actuated_issue") as raise_mock:
        await select._verify_actuation()

    raise_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _do_revert (auto-revert timeout)
# ---------------------------------------------------------------------------


async def test_do_revert_writes_self_consumption_and_stops_heartbeat(hass: HomeAssistant):
    """The auto-revert writes Self-consumption and stops the heartbeat."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE

    with (
        patch("custom_components.sungrow.select.async_stop_heartbeat", new_callable=AsyncMock) as stop_mock,
        patch.object(select, "async_write_ha_state"),
    ):
        await select._do_revert()

    # Wrote the self-consumption payload.
    select.control.async_update_parameters.assert_awaited_once()
    call_payload = select.control.async_update_parameters.call_args.args[1]
    assert "energy_management_mode" in call_payload
    # Heartbeat stopped.
    stop_mock.assert_awaited_once()
    # State updated.
    assert select._attr_current_option == BATTERY_MODE_SELF_CONSUMPTION


async def test_do_revert_skipped_when_removed(hass: HomeAssistant):
    """If the entity was removed, revert is a no-op."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_CHARGE
    select._removed = True

    await select._do_revert()

    select.control.async_update_parameters.assert_not_awaited()


async def test_do_revert_handles_write_failure_gracefully(hass: HomeAssistant):
    """A write error during revert is logged, not raised."""
    select = _make_select(hass)
    select._attr_current_option = BATTERY_MODE_FORCE_DISCHARGE
    select.control.async_update_parameters = AsyncMock(side_effect=PySolarCloudException("timeout"))

    with (
        patch("custom_components.sungrow.select.async_stop_heartbeat", new_callable=AsyncMock),
        patch.object(select, "async_write_ha_state"),
    ):
        # Should not raise.
        await select._do_revert()

    # Still sets the state to safe mode.
    assert select._attr_current_option == BATTERY_MODE_SELF_CONSUMPTION
