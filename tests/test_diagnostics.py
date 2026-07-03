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
    # The App ID is a stable identifier and is redacted.
    assert diag["app_id"] == "**REDACTED**"
    assert diag["tokens_present"] is True
    assert "access_token" not in diag
    assert diag["options"] == {"scan_interval": 10}

    plant = diag["plants"]["123"]
    assert plant["plant_name"] == "Test Plant"
    assert plant["data"]["total_active_power"]["value"] == "1.23"
    # The dispatch-discovered subset survives, but the device uuid is redacted.
    assert plant["devices"] == [{"uuid": "**REDACTED**", "device_name": "Inverter"}]
    # The full device list surfaces the unmapped charger with its raw type id, and
    # serialises the known enum device type to a readable "NAME (value)" string.
    # (Look up by device_name since the uuid is now redacted.)
    charger = next(d for d in plant["all_devices"] if d["device_name"] == "AC011E")
    assert charger["device_type"] == 999
    assert charger["uuid"] == "**REDACTED**"
    ess = next(d for d in plant["all_devices"] if d["device_name"] == "Inverter")
    assert ess["device_type"] == "ENERGY_STORAGE_SYSTEM (14)"
    # Per-device realtime is captured, keyed by device type id; the per-device
    # uuid key is anonymised to a stable device_N placeholder (#122).
    assert "chg-1" not in plant["device_realtime"]["999"]
    assert plant["device_realtime"]["999"]["device_1"]["ev_charger_power"]["value"] == "7.2"

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


async def test_diagnostics_anonymises_device_realtime_uuid_keys(hass: HomeAssistant):
    """Per-device realtime is keyed by device uuid; those uuid keys must not leak (#122).

    ``async_redact_data`` only scrubs values under known key names, so device uuids used
    as dict *keys* need explicit anonymisation to stable ``device_N`` placeholders.
    """
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[
            {"uuid": "ess-aaaa", "device_name": "ESS", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM},
            {"uuid": "chg-bbbb", "device_name": "Charger", "device_type": 999},
        ]
    )

    async def _fake_realtime(plant_id, device_type):
        if getattr(device_type, "value", device_type) == DeviceType.ENERGY_STORAGE_SYSTEM.value:
            # Two batteries of the same type -> two distinct uuid keys.
            return {
                "ess-aaaa": {"battery_level_soc": {"code": "battery_level_soc", "value": "55"}},
                "batt-cccc": {"battery_level_soc": {"code": "battery_level_soc", "value": "60"}},
            }
        return {"chg-bbbb": {"ev_charger_power": {"code": "ev_charger_power", "value": "7.2"}}}

    service.async_get_device_realtime = AsyncMock(side_effect=_fake_realtime)
    coordinator = _make_coordinator("1", "P", {}, plants_service=service)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)
    realtime = diag["plants"]["1"]["device_realtime"]

    # No raw device uuid appears anywhere in the serialised diagnostics payload.
    dumped = json.dumps(diag)
    for raw in ("ess-aaaa", "batt-cccc", "chg-bbbb"):
        assert raw not in dumped

    ess_type = str(DeviceType.ENERGY_STORAGE_SYSTEM.value)
    # Both same-type devices get distinct stable placeholders; their points survive.
    assert set(realtime[ess_type]) == {"device_1", "device_2"}
    soc_values = {realtime[ess_type][k]["battery_level_soc"]["value"] for k in realtime[ess_type]}
    assert soc_values == {"55", "60"}
    # A device of a different type continues the same counter (no collision).
    assert list(realtime["999"]) == ["device_3"]
    assert realtime["999"]["device_3"]["ev_charger_power"]["value"] == "7.2"


async def test_diagnostics_redacts_hardware_identifiers(hass: HomeAssistant):
    """uuid, ps_key and serial numbers are redacted; ps_id and useful fields survive (#114)."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {
        "gateway": "Europe",
        "app_id": "my_app",
        "app_key": "should_never_appear",
        "tokens": {"access_token": "secret"},
    }
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[
            {
                "uuid": "dev-1",
                "device_name": "Inverter",
                "ps_key": "PS_KEY_123",
                "dev_sn": "SN123456",
                "sn": "SN123456",
                "device_sn": "SN123456",
                "ps_id": "123",
            }
        ]
    )
    service.async_get_device_realtime = AsyncMock(return_value={})
    coordinator = _make_coordinator("123", "Test Plant", {}, plants_service=service)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Secrets never appear and the App ID is redacted.
    assert diag["app_id"] == "**REDACTED**"
    assert "app_key" not in diag
    assert "access_token" not in json.dumps(diag)

    device = diag["plants"]["123"]["all_devices"][0]
    for key in ("uuid", "ps_key", "dev_sn", "sn", "device_sn"):
        assert device[key] == "**REDACTED**"
    # Non-sensitive fields survive: the device name and the plant id (ps_id) are
    # kept so a support report stays useful.
    assert device["device_name"] == "Inverter"
    assert device["ps_id"] == "123"
    assert "123" in diag["plants"]  # the plant key itself is preserved
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
