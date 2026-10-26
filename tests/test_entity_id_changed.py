"""In-place entity_id renames (HA 2026.11+) keep the battery-mode registry correct (#515)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from pysolarcloud.control import Control

from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.number import SungrowDispatchNumber, SungrowForcedDispatchDurationNumber
from custom_components.sungrow.select import DISPATCH_SELECTS, SungrowDispatchSelect


@pytest.fixture(autouse=True)
def _base_hook():
    """Provide the core base hook, which only exists from HA 2026.11."""
    with patch.object(Entity, "async_entity_id_changed", create=True):
        yield


def _make_select(hass: HomeAssistant, param: str) -> SungrowDispatchSelect:
    coordinator = MagicMock()
    coordinator.plant_id = "plant_1"
    coordinator.plant_name = "Test Plant"
    coordinator.devices = [{"uuid": "dev-1", "device_type": MagicMock(value=14)}]
    control = MagicMock(spec=Control)
    control.async_update_parameters = AsyncMock(return_value=[])
    device = {"uuid": "dev-1", "device_name": "Inverter", "device_type": MagicMock(value=14)}
    select = SungrowDispatchSelect(coordinator, control, device, param, DISPATCH_SELECTS[param])
    select.hass = hass
    return select


async def test_battery_mode_select_rekeyed_on_rename(hass: HomeAssistant):
    """The registry entry moves from the old entity_id to the new one."""
    select = _make_select(hass, "battery_mode")
    select.entity_id = "select.old"
    registry = hass.data.setdefault(DOMAIN, {}).setdefault("battery_mode_selects", {})
    registry["select.old"] = select

    select.entity_id = "select.new"
    select.async_entity_id_changed("select.old")

    assert registry == {"select.new": select}


async def test_other_select_does_not_touch_registry(hass: HomeAssistant):
    """Non battery-mode selects never register, so a rename leaves the registry alone."""
    select = _make_select(hass, "feed_in_limitation")
    select.entity_id = "select.new"

    select.async_entity_id_changed("select.old")

    assert "battery_mode_selects" not in hass.data.get(DOMAIN, {})


@pytest.mark.parametrize("cls", [SungrowDispatchNumber, SungrowForcedDispatchDurationNumber])
def test_numbers_opt_in_to_in_place_rename(cls):
    """Both number classes define the hook so HA 2026.11+ renames them in place."""
    assert "async_entity_id_changed" in cls.__dict__
