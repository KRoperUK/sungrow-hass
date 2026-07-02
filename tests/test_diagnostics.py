"""Tests for the Sungrow diagnostics platform."""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.sungrow import SungrowData
from custom_components.sungrow.diagnostics import async_get_config_entry_diagnostics


def _make_coordinator(plant_id: str, plant_name: str, data: dict):
    """Build a minimal coordinator mock."""
    coordinator = MagicMock()
    coordinator.plant_id = plant_id
    coordinator.plant_name = plant_name
    coordinator.last_update_success = True
    coordinator.data = data
    return coordinator


async def test_config_entry_diagnostics(hass: HomeAssistant):
    """Diagnostics include plant data and device lists without tokens."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        "gateway": "Europe",
        "app_id": "my_app",
        "tokens": {"access_token": "secret", "refresh_token": "secret"},
    }
    entry.options = {"scan_interval": 10}

    coordinator = _make_coordinator("123", "Test Plant", {"total_active_power": {"value": "1.23"}})

    entry.runtime_data = SungrowData(
        coordinators=[coordinator],
        control=MagicMock(),
        devices={"123": [{"uuid": "dev-1", "device_name": "Inverter"}]},
    )

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry_id"] == "test_entry"
    assert diag["gateway"] == "Europe"
    assert diag["app_id"] == "my_app"
    assert diag["tokens_present"] is True
    assert "access_token" not in diag
    assert diag["options"] == {"scan_interval": 10}
    assert diag["plants"]["123"]["plant_name"] == "Test Plant"
    assert diag["plants"]["123"]["data"]["total_active_power"]["value"] == "1.23"
    assert diag["plants"]["123"]["devices"] == [{"uuid": "dev-1", "device_name": "Inverter"}]


async def test_config_entry_diagnostics_no_data(hass: HomeAssistant):
    """Diagnostics handle a config entry with no stored runtime data."""
    entry = MagicMock()
    entry.entry_id = "missing"
    entry.data = {"gateway": "Europe", "app_id": "my_app"}
    entry.options = {}

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["tokens_present"] is False
    assert diag["plants"] == {}
