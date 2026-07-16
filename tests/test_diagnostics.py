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
    # plant_name and device_name are redacted — they often encode the user's address.
    assert plant["plant_name"] == "**REDACTED**"
    assert plant["data"]["total_active_power"]["value"] == "1.23"
    # The dispatch-discovered subset survives, but the uuid and name are redacted.
    assert plant["devices"] == [{"uuid": "**REDACTED**", "device_name": "**REDACTED**"}]
    # The full device list surfaces the unmapped charger with its raw type id, and
    # serialises the known enum device type to a readable "NAME (value)" string.
    # (Look up by device_type since the uuid and name are now redacted.)
    charger = next(d for d in plant["all_devices"] if d["device_type"] == 999)
    assert charger["uuid"] == "**REDACTED**"
    assert charger["device_name"] == "**REDACTED**"
    ess = next(d for d in plant["all_devices"] if d["device_type"] == "ENERGY_STORAGE_SYSTEM (14)")
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
                "communication_dev_sn": "EXAMPLE-SN-0002",
                "ps_id": "123",
                "ps_name": "7 Acacia Avenue",  # address-encoding name must be redacted
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
    # Hardware identifiers AND the user-set names (which often encode a home address)
    # are redacted.
    for key in ("uuid", "ps_key", "dev_sn", "sn", "device_sn", "communication_dev_sn", "device_name", "ps_name"):
        assert device[key] == "**REDACTED**"
    assert "7 Acacia Avenue" not in json.dumps(diag)
    assert diag["plants"]["123"]["plant_name"] == "**REDACTED**"
    # Non-sensitive fields survive so a support report stays useful: the plant id
    # (ps_id) and the hardware model/type.
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


async def test_points_catalog_lists_points_per_device(hass: HomeAssistant):
    """The points_catalog flattens plant + per-device points into pickable rows (#252)."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[
            {
                "uuid": "ess-1",
                "device_name": "My Inverter",
                "device_type": DeviceType.ENERGY_STORAGE_SYSTEM,
                "device_model_code": "SH10RT-20",
            },
        ]
    )
    service.async_get_device_realtime = AsyncMock(
        return_value={
            "ess-1": {
                "battery_charge_power": {"id": "13126", "code": "battery_charge_power", "value": "800", "unit": "W"},
                "operating_status": {"id": "13146", "code": "operating_status", "value": "13"},
            }
        }
    )
    coordinator = _make_coordinator(
        "1",
        "P",
        {"total_active_power": {"id": "83022", "code": "total_active_power", "value": "5.2", "unit": "kW"}},
        plants_service=service,
    )
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)
    catalog = diag["plants"]["1"]["points_catalog"]

    # Plant-level points are listed with id/code/value/unit.
    plant_ids = {row["point_id"] for row in catalog["plant"]}
    assert "83022" in plant_ids

    ess_type = str(DeviceType.ENERGY_STORAGE_SYSTEM.value)
    device = catalog["devices"][ess_type]
    # The device is annotated with its resolved family/battery capability (#251).
    assert device["model"] == "SH10RT-20"
    assert device["family"] == "sh_rt"
    assert device["has_battery"] is True
    rows = {row["point_id"]: row for row in device["points"]}
    assert rows["13126"]["code"] == "battery_charge_power"
    assert rows["13126"]["unit"] == "W"
    assert "13146" in rows
    # Rows are sorted by point id for stable, scannable output.
    assert [r["point_id"] for r in device["points"]] == sorted(rows)
    json.dumps(diag)


async def test_points_catalog_contains_no_pii(hass: HomeAssistant):
    """The catalog carries only point metadata + model code — no uuids/serials/names (#252)."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[
            {
                "uuid": "secret-uuid",
                "device_name": "7 Acacia Avenue Inverter",
                "device_sn": "SN-SECRET",
                "device_type": DeviceType.INVERTER,
                "device_model_code": "SG3.6RS",
            }
        ]
    )
    service.async_get_device_realtime = AsyncMock(
        return_value={
            "secret-uuid": {"mppt1_voltage": {"id": "5", "code": "mppt1_voltage", "value": "320", "unit": "V"}}
        }
    )
    coordinator = _make_coordinator("1", "P", {}, plants_service=service)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)
    catalog = diag["plants"]["1"]["points_catalog"]

    dumped = json.dumps(catalog)
    for secret in ("secret-uuid", "SN-SECRET", "7 Acacia Avenue"):
        assert secret not in dumped
    # A string inverter is correctly flagged as having no battery.
    inv = catalog["devices"][str(DeviceType.INVERTER.value)]
    assert inv["has_battery"] is False
    assert inv["model"] == "SG3.6RS"


async def test_points_catalog_handles_probe_error(hass: HomeAssistant):
    """A per-device probe error yields an empty point list for that type, never a crash (#252)."""
    entry = MagicMock()
    entry.entry_id = "e"
    entry.data = {"gateway": "Europe", "app_id": "a"}
    entry.options = {}

    service = MagicMock()
    service.async_get_plant_devices = AsyncMock(
        return_value=[{"uuid": "chg-1", "device_type": 999, "device_model_code": "AC011E"}]
    )
    service.async_get_device_realtime = AsyncMock(side_effect=RuntimeError("nope"))
    coordinator = _make_coordinator("1", "P", None, plants_service=service)
    entry.runtime_data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={})

    diag = await async_get_config_entry_diagnostics(hass, entry)
    catalog = diag["plants"]["1"]["points_catalog"]

    # Plant data was None -> empty plant list; the errored type has no points but is
    # still listed (as an unknown family, since AC011E isn't an SG/SH inverter).
    assert catalog["plant"] == []
    charger = catalog["devices"]["999"]
    assert charger["points"] == []
    assert charger["family"] == "unknown"
    json.dumps(diag)
