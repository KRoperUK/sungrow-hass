"""Tests for the Sungrow diagnostics platform."""

import json
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from pysolarcloud.plants import DeviceType

from custom_components.sungrow import SungrowData
from custom_components.sungrow.diagnostics import async_get_config_entry_diagnostics


def _make_coordinator(plant_id: str, plant_name: str, data: dict, *, plants_service=None):
    """Build a minimal coordinator mock."""
    coordinator = MagicMock()
    coordinator.plant_id = plant_id
    coordinator.plant_name = plant_name
    coordinator.last_update_success = True
    coordinator.data = data
    coordinator.plants_service = plants_service
    return coordinator


async def test_config_entry_diagnostics(hass: HomeAssistant):
    """Diagnostics include plant data, the full device list, and per-device realtime."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {
        "gateway": "Europe",
        "app_id": "my_app",
        "tokens": {"access_token": "secret", "refresh_token": "secret"},
    }
    entry.options = {"scan_interval": 10}

    # A plant with a known ESS device (converted to an enum by the library) and an
    # unmapped EV charger (an unknown type the library leaves as a raw int).
    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[
            {"uuid": "dev-1", "device_name": "Inverter", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM},
            {"uuid": "chg-1", "device_name": "AC011E", "device_type": 999},
        ]
    )

    async def _fake_realtime(plant_id, device_type):
        if getattr(device_type, "value", device_type) == 999:
            return {"chg-1": {"ev_charger_power": {"code": "ev_charger_power", "value": "7.2"}}}
        return {}

    service.async_get_device_realtime = AsyncMock(side_effect=_fake_realtime)

    coordinator = _make_coordinator(
        "123", "Test Plant", {"total_active_power": {"value": "1.23"}}, plants_service=service
    )

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

    plant = diag["plants"]["123"]
    assert plant["plant_name"] == "Test Plant"
    assert plant["data"]["total_active_power"]["value"] == "1.23"
    # The dispatch-discovered subset is unchanged.
    assert plant["devices"] == [{"uuid": "dev-1", "device_name": "Inverter"}]
    # The full device list surfaces the unmapped charger with its raw type id, and
    # serialises the known enum device type to a readable "NAME (value)" string.
    charger = next(d for d in plant["all_devices"] if d["uuid"] == "chg-1")
    assert charger["device_type"] == 999
    ess = next(d for d in plant["all_devices"] if d["uuid"] == "dev-1")
    assert ess["device_type"] == "ENERGY_STORAGE_SYSTEM (14)"
    # Per-device realtime is captured, keyed by device type id.
    assert plant["device_realtime"]["999"]["chg-1"]["ev_charger_power"]["value"] == "7.2"

    # The whole payload must be JSON-serialisable (no leftover enums).
    json.dumps(diag)


async def test_config_entry_diagnostics_no_data(hass: HomeAssistant):
    """Diagnostics handle a config entry with no stored runtime data."""
    entry = MagicMock()
    entry.entry_id = "missing"
    entry.data = {"gateway": "Europe", "app_id": "my_app"}
    entry.options = {}

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["tokens_present"] is False
    assert diag["plants"] == {}


async def test_diagnostics_device_probe_failure_is_captured(hass: HomeAssistant):
    """A device-listing failure is captured in the dump, never raised."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(side_effect=RuntimeError("boom"))
    coordinator = _make_coordinator("1", "P", {}, plants_service=service)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["plants"]["1"]["all_devices"] == [{"error": "boom"}]
    assert diag["plants"]["1"]["device_realtime"] == {}
    json.dumps(diag)


async def test_diagnostics_per_device_realtime_failure_is_captured(hass: HomeAssistant):
    """A per-device realtime failure is captured per type; malformed devices are skipped."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[
            "not-a-dict",  # skipped
            {"uuid": "x", "device_name": "No type"},  # no device_type -> skipped
            {"uuid": "chg-1", "device_type": 999},
        ]
    )
    service.async_get_device_realtime = AsyncMock(side_effect=RuntimeError("nope"))
    coordinator = _make_coordinator("1", "P", {}, plants_service=service)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["plants"]["1"]["device_realtime"]["999"] == {"error": "nope"}
    json.dumps(diag)


async def test_diagnostics_without_service_skips_probe(hass: HomeAssistant):
    """A coordinator without a plants_service yields empty device sections (no crash)."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    coordinator = _make_coordinator("1", "P", {}, plants_service=None)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["plants"]["1"]["all_devices"] == []
    assert diag["plants"]["1"]["device_realtime"] == {}
