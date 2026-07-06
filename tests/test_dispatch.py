"""Tests for the Sungrow dispatch (number/select) platforms."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pysolarcloud import PySolarCloudException
from pysolarcloud.plants import DeviceType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import (
    SungrowData,
    _async_has_battery,
    _has_battery_device,
    build_device_info,
    select_dispatch_device,
)
from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.number import DISPATCH_NUMBERS
from custom_components.sungrow.number import async_setup_entry as number_setup_entry
from custom_components.sungrow.select import DISPATCH_SELECTS
from custom_components.sungrow.select import async_setup_entry as select_setup_entry

from .conftest import MOCK_CONFIG_DATA


def _coordinator_with(plant_id, plant_name):
    coordinator = MagicMock()
    coordinator.plant_id = plant_id
    coordinator.plant_name = plant_name
    coordinator.config_entry = MagicMock()
    coordinator.last_update_success = True
    # Default to a battery plant so the full dispatch set is built; the no-battery
    # gating tests (#148) override this to False.
    coordinator.has_battery = True
    # Auto-revert off by default (a MagicMock would otherwise read as a truthy duration
    # and arm the timer); the #157 revert tests set a real value.
    coordinator.forced_dispatch_duration_minutes = 0
    return coordinator


def _setup_entry_data(entry, devices) -> SungrowData:
    """Build a SungrowData and attach it to the entry as runtime_data."""
    control = MagicMock()
    control.async_update_parameters = AsyncMock(return_value=[])
    control.async_heartbeat = AsyncMock()
    control.heartbeat_loop = AsyncMock()
    coordinator = _coordinator_with("12345", "Test Plant")
    # Dispatch platforms read the live device list from the coordinator.
    coordinator.devices = devices
    data = SungrowData(
        coordinators=[coordinator],
        control=control,
        devices={"12345": devices},
    )
    entry.runtime_data = data
    return data


async def test_number_setup_creates_entities_for_ess_device(hass: HomeAssistant):
    """Dispatch numbers are created when an ESS device is discovered."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM", "device_name": "Inverter 1"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == len(DISPATCH_NUMBERS) + 1  # + forced_dispatch_duration (#157)
    power = next(e for e in added if e.param == "charge_discharge_power")
    assert power._attr_device_class == NumberDeviceClass.POWER
    assert power._attr_native_unit_of_measurement == "W"


def test_select_dispatch_device_matches_all_representations():
    """The ESS is chosen whether device_type is an enum, int, or string."""
    assert select_dispatch_device([]) is None
    # Inverter first, ESS second — the ESS must still win (not just devices[0]).
    enum_devices = [
        {"uuid": "inv", "device_type": DeviceType.INVERTER},
        {"uuid": "ess", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM},
    ]
    assert select_dispatch_device(enum_devices)["uuid"] == "ess"
    assert select_dispatch_device([{"uuid": "ess", "device_type": 14}])["uuid"] == "ess"
    assert select_dispatch_device([{"uuid": "ess", "device_type": "ENERGY_STORAGE_SYSTEM"}])["uuid"] == "ess"
    # No ESS -> first device.
    assert select_dispatch_device([{"uuid": "inv", "device_type": DeviceType.INVERTER}])["uuid"] == "inv"


async def test_number_setup_prefers_ess_with_enum_device_type(hass: HomeAssistant):
    """ESS is selected even when device_type is a DeviceType enum (the production path).

    Regression test: the old string comparison never matched the enum, so dispatch
    silently fell back to devices[0] (here the inverter).
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [
        {"uuid": "inv-1", "device_type": DeviceType.INVERTER},
        {"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM},
    ]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added
    assert all(e.device_uuid == "ess-1" for e in added)


async def test_number_setup_falls_back_to_inverter(hass: HomeAssistant):
    """Dispatch numbers fall back to an inverter device if no ESS device exists."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-2", "device_type": "INVERTER"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == len(DISPATCH_NUMBERS) + 1  # + forced_dispatch_duration (#157)


@pytest.mark.parametrize(("uuid_in", "uuid_str"), [(4841885, "4841885"), ("already-str", "already-str")])
async def test_dispatch_device_identifier_is_string(hass: HomeAssistant, uuid_in, uuid_str):
    """Device identifiers are strings even when the API returns an int uuid.

    Regression for the "inverter device pops in then disappears on reload" bug: the
    API returns device uuids as ints, but `_known_device_ids` keys on `str(uuid)`, so
    an int identifier never matches and `_async_prune_stale_devices` deletes the
    just-created device on the next refresh.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": uuid_in, "device_type": DeviceType.INVERTER, "device_name": "Inverter1"}]
    _setup_entry_data(entry, devices)

    added_numbers: list = []
    await number_setup_entry(hass, entry, lambda entities: added_numbers.extend(entities))
    added_selects: list = []
    await select_setup_entry(hass, entry, lambda entities: added_selects.extend(entities))

    assert added_numbers and added_selects
    for entity in [*added_numbers, *added_selects]:
        assert entity.device_uuid == uuid_str
        assert entity._attr_device_info["identifiers"] == {(DOMAIN, uuid_str)}
        assert entity._attr_device_info["via_device"] == (DOMAIN, "12345")


async def test_dispatch_device_survives_prune_with_int_uuid(hass: HomeAssistant):
    """A dispatch device built from an int uuid is not pruned on the next refresh.

    End-to-end guard: registers the device with the exact identifiers the entity
    produces, then runs the prune that fires after every coordinator refresh. Before
    the fix the int-vs-str mismatch removed it (pop-in-then-disappear).
    """
    from homeassistant.helpers import device_registry as dr

    from custom_components.sungrow import _async_prune_stale_devices

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="app")
    entry.add_to_hass(hass)
    devices = [{"uuid": 4841885, "device_type": DeviceType.INVERTER, "device_name": "Inverter1"}]
    _setup_entry_data(entry, devices)

    added: list = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert added
    identifiers = added[0]._attr_device_info["identifiers"]

    registry = dr.async_get(hass)
    plant = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "12345")})
    inverter = registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers=identifiers)

    _async_prune_stale_devices(hass, entry)

    assert registry.async_get(plant.id) is not None
    assert registry.async_get(inverter.id) is not None  # removed before the fix


# --- Battery-presence gating (#148) ---------------------------------------

# Dispatch params that only make sense with a battery and must be hidden on
# PV-only plants (setting them puts the inverter into External-EMS "Dispatched
# running" mode and silently curtails PV to ~0).
BATTERY_ONLY_NUMBERS = {
    "charge_discharge_power",
    "soc_upper_limit",
    "soc_lower_limit",
    "forced_charging_target_soc_1",
    "forced_charging_target_soc_2",
}
GRID_SIDE_NUMBERS = {
    "feed_in_limitation_value",
    "feed_in_limitation_ratio",
    "active_power_limit_ratio",
    "q_t",
    "pf",
}
BATTERY_ONLY_SELECTS = {"charge_discharge_command", "forced_charging", "battery_first"}
GRID_SIDE_SELECTS = {"feed_in_limitation", "limited_power_switch", "reactive_power_regulation_mode"}


def test_battery_only_sets_partition_dispatch_dicts():
    """The battery-only / grid-side split covers exactly the dispatch params.

    Guards against a new dispatch param being added without deciding whether it is
    battery-only (and therefore must be gated for #148).
    """
    assert set(DISPATCH_NUMBERS) == BATTERY_ONLY_NUMBERS | GRID_SIDE_NUMBERS
    assert BATTERY_ONLY_NUMBERS.isdisjoint(GRID_SIDE_NUMBERS)
    assert set(DISPATCH_SELECTS) == BATTERY_ONLY_SELECTS | GRID_SIDE_SELECTS
    assert BATTERY_ONLY_SELECTS.isdisjoint(GRID_SIDE_SELECTS)


async def test_number_hides_battery_controls_without_battery(hass: HomeAssistant):
    """On a battery-less plant, only grid-side numbers are created (#148)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "inv-1", "device_type": "INVERTER"}]
    data = _setup_entry_data(entry, devices)
    data.coordinators[0].has_battery = False

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert {e.param for e in added} == GRID_SIDE_NUMBERS


async def test_select_hides_battery_controls_without_battery(hass: HomeAssistant):
    """On a battery-less plant, only grid-side selects are created (#148)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "inv-1", "device_type": "INVERTER"}]
    data = _setup_entry_data(entry, devices)
    data.coordinators[0].has_battery = False

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert {e.param for e in added} == GRID_SIDE_SELECTS


async def test_dispatch_full_set_with_battery(hass: HomeAssistant):
    """A battery plant still gets the full set of dispatch controls (#148)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    data = _setup_entry_data(entry, devices)
    data.coordinators[0].has_battery = True

    numbers, selects = [], []
    await number_setup_entry(hass, entry, lambda e: numbers.extend(e))
    await select_setup_entry(hass, entry, lambda e: selects.extend(e))

    assert {e.param for e in numbers} == set(DISPATCH_NUMBERS) | {"forced_dispatch_duration"}
    assert {e.param for e in selects} == set(DISPATCH_SELECTS)


def test_has_battery_device_detects_ess_and_battery():
    """_has_battery_device matches ESS/battery devices across enum/int/string forms."""
    assert _has_battery_device([{"uuid": "e", "device_type": "ENERGY_STORAGE_SYSTEM"}])
    assert _has_battery_device([{"uuid": "b", "device_type": DeviceType.BATTERY}])
    assert _has_battery_device([{"uuid": "b", "device_type": 43}])
    assert not _has_battery_device([{"uuid": "i", "device_type": "INVERTER"}])
    assert not _has_battery_device([])


async def test_async_has_battery_uses_design_capacity():
    """design_capacity_battery is authoritative: 0 -> no battery, >0 -> battery."""
    svc = MagicMock()
    svc.async_get_plant_details = AsyncMock(return_value=[{"design_capacity_battery": 0.0}])
    assert await _async_has_battery(svc, "1", [{"uuid": "i", "device_type": "INVERTER"}]) is False

    svc.async_get_plant_details = AsyncMock(return_value=[{"design_capacity_battery": 9.6}])
    assert await _async_has_battery(svc, "1", []) is True


async def test_async_has_battery_capacity_zero_overrides_ess_device():
    """A configured capacity of 0 hides controls even if an ESS device is present.

    Hybrid inverters can report an ESS device with no battery pack attached, so the
    plant's configured capacity is trusted over device-type presence.
    """
    svc = MagicMock()
    svc.async_get_plant_details = AsyncMock(return_value=[{"design_capacity_battery": 0}])
    assert await _async_has_battery(svc, "1", [{"uuid": "e", "device_type": "ENERGY_STORAGE_SYSTEM"}]) is False


async def test_async_has_battery_falls_back_to_devices_on_error():
    """If plant details can't be fetched, fall back to ESS/battery device presence."""
    svc = MagicMock()
    svc.async_get_plant_details = AsyncMock(side_effect=PySolarCloudException("boom"))
    assert await _async_has_battery(svc, "1", [{"uuid": "e", "device_type": "ENERGY_STORAGE_SYSTEM"}]) is True
    assert await _async_has_battery(svc, "1", [{"uuid": "i", "device_type": "INVERTER"}]) is False


# --- Device-registry metadata (#149) --------------------------------------


def test_build_device_info_enriches_model_and_serial():
    """build_device_info surfaces the cloud's model/serial/manufacturer and str-ifies the uuid."""
    info = build_device_info(
        {
            "uuid": 4841885,
            "device_name": "7-Tadmore-Close-Inverter",
            "device_model_code": "SG3.6RS",
            "device_sn": "A2281821940",
            "factory_name": "SUNGROW",
        },
        "5718745",
        fallback_name="Plant",
    )
    assert info["identifiers"] == {(DOMAIN, "4841885")}
    assert info["name"] == "7-Tadmore-Close-Inverter"
    assert info["model"] == "SG3.6RS"
    assert info["serial_number"] == "A2281821940"
    assert info["manufacturer"] == "SUNGROW"
    assert info["via_device"] == (DOMAIN, "5718745")


def test_build_device_info_falls_back_when_fields_absent():
    """Missing name falls back to the plant name; manufacturer defaults to Sungrow."""
    info = build_device_info({"uuid": "x"}, "p", fallback_name="Plant Name")
    assert info["name"] == "Plant Name"
    assert info["manufacturer"] == "Sungrow"
    assert info.get("model") is None
    assert info.get("serial_number") is None


async def test_dispatch_device_info_carries_model_and_serial(hass: HomeAssistant):
    """Dispatch entities expose the device's model/serial on the device card (#149)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [
        {
            "uuid": "inv-1",
            "device_type": "ENERGY_STORAGE_SYSTEM",
            "device_name": "Inverter1",
            "device_model_code": "SH10RT",
            "device_sn": "SN123",
            "factory_name": "SUNGROW",
        }
    ]
    _setup_entry_data(entry, devices)

    added: list = []
    await number_setup_entry(hass, entry, lambda e: added.extend(e))

    assert added
    info = added[0]._attr_device_info
    assert info["model"] == "SH10RT"
    assert info["serial_number"] == "SN123"
    assert info["manufacturer"] == "SUNGROW"


async def test_number_setup_no_devices(hass: HomeAssistant):
    """No dispatch numbers are created when no dispatchable devices are found."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [])

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []


async def test_number_setup_skips_device_without_uuid(hass: HomeAssistant):
    """A discovered device with no uuid is skipped (no entities created)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"device_type": "ENERGY_STORAGE_SYSTEM", "device_name": "No UUID"}])

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []


async def test_select_setup_skips_device_without_uuid(hass: HomeAssistant):
    """A discovered device with no uuid is skipped (no select entities created)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"device_type": "ENERGY_STORAGE_SYSTEM", "device_name": "No UUID"}])

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []


async def test_number_set_value_calls_control(hass: HomeAssistant):
    """Setting a number entity writes the parameter via Control."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")

    await power.async_set_native_value(2500)

    # Power is sent verbatim in watts.
    entry_data.control.async_update_parameters.assert_awaited_once_with(
        "dev-uuid-1", {"charge_discharge_power": "2500"}
    )


async def test_number_power_does_not_arm_heartbeat(hass: HomeAssistant):
    """Writing charge/discharge power never arms the EMS heartbeat (#112).

    The heartbeat is owned solely by the command select (Charge/Discharge start it,
    Stop stops it); the power number just writes the power parameter. Writing a
    non-zero power — and writing 0 — must both leave the heartbeat untouched.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    data = _setup_entry_data(entry, devices)
    # Point the coordinator at the real entry so any armed heartbeat would be observable.
    data.coordinators[0].config_entry = entry

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")
    power.hass = hass

    await power.async_set_native_value(1500)
    assert data.heartbeats == {}  # non-zero power must not start a heartbeat
    await power.async_set_native_value(0)
    assert data.heartbeats == {}  # ...and neither does zero
    data.control.async_update_parameters.assert_awaited()


async def test_number_availability_follows_coordinator(hass: HomeAssistant):
    """Number availability tracks the coordinator; native_value is None with no side effects."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")

    assert power.native_value is None
    assert power.available is True  # coordinator.last_update_success is True

    power.coordinator.last_update_success = False
    assert power.available is False
    # Reading native_value must not flip availability back on.
    assert power.native_value is None
    assert power.available is False


async def test_select_setup_creates_entities_for_ess_device(hass: HomeAssistant):
    """Dispatch selects are created when an ESS device is discovered."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == len(DISPATCH_SELECTS)
    command = next(e for e in added if e.param == "charge_discharge_command")
    assert isinstance(command, SelectEntity)
    assert set(command.options_map.keys()) == {"Stop", "Charge", "Discharge"}


async def test_select_option_calls_control(hass: HomeAssistant):
    """Selecting an option writes the parameter via Control."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _setup_entry_data(entry, devices)

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")

    with patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()):
        await command.async_select_option("Charge")

    entry_data.control.async_update_parameters.assert_awaited_once_with(
        "dev-uuid-1", {"charge_discharge_command": "170"}
    )


async def test_select_stop_stops_heartbeat(hass: HomeAssistant):
    """Selecting 'Stop' stops the EMS heartbeat loop."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")

    command.hass = hass
    with patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await command.async_select_option("Stop")

    mock_stop.assert_awaited_once_with(hass, command.coordinator.config_entry, "12345")


async def test_select_availability_follows_coordinator(hass: HomeAssistant):
    """Select availability tracks the coordinator; current_option is None with no side effects."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")

    assert command.current_option is None
    assert command.available is True

    command.coordinator.last_update_success = False
    assert command.available is False
    assert command.current_option is None
    assert command.available is False


async def test_removing_one_entity_keeps_plant_heartbeat(hass: HomeAssistant):
    """Removing a single dispatch entity must not stop the shared plant heartbeat (#112).

    All ~13 dispatch entities share one heartbeat keyed by plant_id. Disabling or
    removing one (e.g. "SOC Upper Limit") mid-charge must not kill dispatch for the
    whole plant; teardown is handled once by async_unload_entry (see
    test_unload_cancels_running_heartbeat in test_init.py).
    """
    import asyncio

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    data = _setup_entry_data(entry, devices)

    numbers: list = []
    await number_setup_entry(hass, entry, lambda entities: numbers.extend(entities))
    selects: list = []
    await select_setup_entry(hass, entry, lambda entities: selects.extend(entities))

    # A heartbeat is active for the plant.
    data.heartbeats["12345"] = (asyncio.Event(), MagicMock())

    with patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()) as sel_stop:
        for entity in (numbers[0], selects[0]):
            entity.hass = hass
            await entity.async_will_remove_from_hass()

    sel_stop.assert_not_awaited()
    assert "12345" in data.heartbeats  # heartbeat survives single-entity removal


def test_select_dispatch_device_ignores_non_dispatch_devices():
    """Meters / EV chargers are never chosen for dispatch (only inverter/ESS)."""
    # A non-dispatch device alone -> nothing dispatch-capable.
    assert select_dispatch_device([{"uuid": "meter", "device_type": DeviceType.METER}]) is None
    # Meter listed first, inverter second -> the inverter wins (not devices[0]).
    devices = [
        {"uuid": "meter", "device_type": DeviceType.METER},
        {"uuid": "inv", "device_type": DeviceType.INVERTER},
    ]
    assert select_dispatch_device(devices)["uuid"] == "inv"


# ---------------------------------------------------------------------------
# Rated power derived from model code (issue #81)
# ---------------------------------------------------------------------------


def test_rated_power_w_parses_model_codes():
    """Rated power is parsed from Sungrow model codes; non-inverters return None."""
    from custom_components.sungrow.number import rated_power_w

    assert rated_power_w({"device_model_code": "SH10RT-V112"}) == 10000
    assert rated_power_w({"device_model_code": "SG3.6RS"}) == 3600
    assert rated_power_w({"device_model_code": "SG110CX"}) == 110000
    assert rated_power_w({"device_model_code": "SBR256"}) is None  # battery
    assert rated_power_w({"device_model_code": "SGSmartMeter"}) is None  # meter
    assert rated_power_w({}) is None
    # Nonsense parses are rejected by the sanity guard (0 kW or absurdly large).
    assert rated_power_w({"device_model_code": "SG0RS"}) is None  # parses to 0 kW
    assert rated_power_w({"device_model_code": "SG9999"}) is None  # parses to >1000 kW


async def test_charge_power_max_from_model_code(hass: HomeAssistant):
    """The charge/discharge power slider is sized to the device's rated power."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "device_model_code": "SH10RT-V112"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    power = next(e for e in added if e.param == "charge_discharge_power")
    assert power._attr_native_max_value == 10000
    # Percentage-based params are unaffected.
    soc = next(e for e in added if e.param == "soc_upper_limit")
    assert soc._attr_native_max_value == 100


async def test_charge_power_max_falls_back_to_default(hass: HomeAssistant):
    """An unparseable model code falls back to the default clamp."""
    from custom_components.sungrow.number import DEFAULT_MAX_DISPATCH_POWER

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "ess-1", "device_type": DeviceType.ENERGY_STORAGE_SYSTEM, "device_model_code": "SBR256"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    power = next(e for e in added if e.param == "charge_discharge_power")
    assert power._attr_native_max_value == DEFAULT_MAX_DISPATCH_POWER


# ---------------------------------------------------------------------------
# kW unit conversion + additional controls (device-verified formats)
# ---------------------------------------------------------------------------


async def test_param_write_encodings(hass: HomeAssistant):
    """Values are encoded per Appendix 10: power in watts, SOC/ratios as tenths of a %."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    by_param = {e.param: e for e in added}

    # Power is sent verbatim in watts (not kW).
    await by_param["charge_discharge_power"].async_set_native_value(2500)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"charge_discharge_power": "2500"})

    # SOC limits and ratios are sent as tenths of a percent (x10).
    await by_param["soc_upper_limit"].async_set_native_value(90)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"soc_upper_limit": "900"})

    await by_param["feed_in_limitation_ratio"].async_set_native_value(80)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"feed_in_limitation_ratio": "800"})

    # Forced-charge target SOC is a direct percent (x1).
    await by_param["forced_charging_target_soc_1"].async_set_native_value(75)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"forced_charging_target_soc_1": "75"})

    # The lower SOC bound is also tenths of a percent (x10).
    await by_param["soc_lower_limit"].async_set_native_value(20)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"soc_lower_limit": "200"})

    # Active-power / feed-in *ratio* caps are percentages sent as tenths (x10).
    await by_param["active_power_limit_ratio"].async_set_native_value(60)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"active_power_limit_ratio": "600"})

    # The absolute feed-in limit is a power in watts, sent verbatim (x1).
    await by_param["feed_in_limitation_value"].async_set_native_value(3000)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"feed_in_limitation_value": "3000"})

    # The second forced-charge target SOC mirrors the first: a direct percent (x1).
    await by_param["forced_charging_target_soc_2"].async_set_native_value(80)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"forced_charging_target_soc_2": "80"})

    # Reactive power ratio Q(t) is tenths of a percent, signed (x10) (#181).
    await by_param["q_t"].async_set_native_value(30)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"q_t": "300"})
    await by_param["q_t"].async_set_native_value(-60)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"q_t": "-600"})

    # Power factor is thousandths, signed (x1000) (#181).
    await by_param["pf"].async_set_native_value(0.9)
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"pf": "900"})


async def test_new_selects_write_verified_codes(hass: HomeAssistant):
    """Feed-in / battery-first selects write the device-verified enable/disable codes."""
    from homeassistant.const import EntityCategory

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    by_param = {e.param: e for e in added}

    assert {"feed_in_limitation", "limited_power_switch", "battery_first"} <= by_param.keys()
    await by_param["feed_in_limitation"].async_select_option("Enable")
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"feed_in_limitation": "170"})
    await by_param["battery_first"].async_select_option("Disable")
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"battery_first": "85"})
    assert by_param["battery_first"]._attr_entity_category == EntityCategory.CONFIG

    # Reactive power mode writes the Appendix 10 enum code (#181).
    await by_param["reactive_power_regulation_mode"].async_select_option("Reactive Power Ratio Q(t)")
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"reactive_power_regulation_mode": "162"})
    await by_param["reactive_power_regulation_mode"].async_select_option("Off")
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"reactive_power_regulation_mode": "85"})


# ---------------------------------------------------------------------------
# Dispatch support gating
# ---------------------------------------------------------------------------


async def test_no_controls_when_updates_unsupported(hass: HomeAssistant):
    """No number/select controls are created when the device rejects writes."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])
    data.coordinators[0].dispatch_update_supported = False

    numbers: list = []
    await number_setup_entry(hass, entry, lambda entities: numbers.extend(entities))
    selects: list = []
    await select_setup_entry(hass, entry, lambda entities: selects.extend(entities))

    assert numbers == []
    assert selects == []


async def test_dispatch_supported_check_fails_open():
    """_async_dispatch_supported only returns False on an explicit API 'no'."""
    from custom_components.sungrow import _async_dispatch_supported

    ess = [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    control = MagicMock()

    control.async_check_update_support = AsyncMock(return_value=False)
    assert await _async_dispatch_supported(control, ess) is False

    # No dispatch device yet -> unknown -> fail open (a later device still gets controls).
    assert await _async_dispatch_supported(control, []) is True

    # A failing check must not hide working controls.
    control.async_check_update_support = AsyncMock(side_effect=Exception("boom"))
    assert await _async_dispatch_supported(control, ess) is True


# ---------------------------------------------------------------------------
# Entity categories + state restoration
# ---------------------------------------------------------------------------


async def test_number_config_entities_have_category(hass: HomeAssistant):
    """SOC / forced-charging numbers are config entities; charge power is primary."""
    from homeassistant.const import EntityCategory

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    cats = {e.param: e._attr_entity_category for e in added}

    assert cats["charge_discharge_power"] is None
    assert cats["soc_upper_limit"] == EntityCategory.CONFIG
    assert cats["soc_lower_limit"] == EntityCategory.CONFIG
    assert cats["forced_charging_target_soc_1"] == EntityCategory.CONFIG


async def test_second_forced_charge_window_control(hass: HomeAssistant):
    """The second forced-charge target SOC is exposed as a config entity (0-100%)."""
    from homeassistant.const import EntityCategory

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(
        entry,
        [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM", "device_model_code": "SH10RT-V112"}],
    )

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    by_param = {e.param: e for e in added}

    assert "forced_charging_target_soc_2" in by_param
    assert by_param["forced_charging_target_soc_2"]._attr_native_max_value == 100
    assert by_param["forced_charging_target_soc_2"]._attr_entity_category == EntityCategory.CONFIG
    # The dropped max-charge/discharge-power controls are no longer created.
    assert "max_charging_power" not in by_param
    assert "max_discharging_power" not in by_param


async def test_select_forced_charging_is_config(hass: HomeAssistant):
    """The forced-charging select is a config entity; the command select is primary."""
    from homeassistant.const import EntityCategory

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    cats = {e.param: e._attr_entity_category for e in added}

    assert cats["charge_discharge_command"] is None
    assert cats["forced_charging"] == EntityCategory.CONFIG


async def test_number_restores_last_value(hass: HomeAssistant):
    """The last commanded number value is restored across a restart."""
    from types import SimpleNamespace

    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")
    power.hass = hass
    power.async_get_last_number_data = AsyncMock(return_value=SimpleNamespace(native_value=1234.0))

    with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()):
        await power.async_added_to_hass()

    assert power.native_value == 1234.0


async def test_select_restores_last_option(hass: HomeAssistant):
    """The last selected option is restored across a restart."""
    from homeassistant.core import State
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    # Restore a non-dispatching option so this stays a pure restore test; the
    # Charge/Discharge heartbeat-resume path is covered separately (#112).
    command.async_get_last_state = AsyncMock(return_value=State("select.x", "Stop"))

    with patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()):
        await command.async_added_to_hass()

    assert command.current_option == "Stop"


async def test_restored_charge_command_resumes_heartbeat(hass: HomeAssistant):
    """A restored Charge command restarts the EMS heartbeat after a restart/reload (#112).

    Otherwise the inverter times out of External-EMS mode while the UI still shows
    "Charge" — the command select must restore state AND resume the heartbeat.
    """
    from homeassistant.core import State
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    command.async_get_last_state = AsyncMock(return_value=State("select.x", "Charge"))

    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()),
        patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()) as mock_start,
    ):
        await command.async_added_to_hass()

    assert command.current_option == "Charge"
    mock_start.assert_awaited_once()
    # The heartbeat is started for the restored command's device.
    assert mock_start.call_args.args[3] == "ess-1"


# ---------------------------------------------------------------------------
# Forced-dispatch auto-revert (#157)
# ---------------------------------------------------------------------------


async def test_forced_dispatch_duration_number_is_local(hass: HomeAssistant):
    """The duration number stores its value on the coordinator, writing nothing to the API."""
    from custom_components.sungrow.number import SungrowForcedDispatchDurationNumber

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])
    number = SungrowForcedDispatchDurationNumber(data.coordinators[0], {"uuid": "ess-1"})

    await number.async_set_native_value(30)

    assert number.native_value == 30
    assert data.coordinators[0].forced_dispatch_duration_minutes == 30
    data.control.async_update_parameters.assert_not_called()


async def test_charge_arms_autorevert_and_stop_cancels(hass: HomeAssistant):
    """Selecting Charge arms the auto-revert (with a persisted deadline); Stop cancels it."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])
    data.coordinators[0].forced_dispatch_duration_minutes = 10

    added: list = []
    await select_setup_entry(hass, entry, lambda e: added.extend(e))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass

    with (
        patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()),
        patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()),
    ):
        await command.async_select_option("Charge")
        assert command._revert_deadline is not None
        assert command.extra_state_attributes == {"revert_at": command._revert_deadline}

        await command.async_select_option("Stop")
        assert command._revert_deadline is None
        assert command.extra_state_attributes is None


async def test_autorevert_writes_stop_and_stops_heartbeat(hass: HomeAssistant):
    """When the revert fires it writes Stop, stops the heartbeat and updates the UI (#157)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added: list = []
    await select_setup_entry(hass, entry, lambda e: added.extend(e))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    command.async_write_ha_state = MagicMock()
    command._attr_current_option = "Charge"

    with patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await command._do_revert()

    data.control.async_update_parameters.assert_awaited_with("ess-1", {"charge_discharge_command": "204"})
    mock_stop.assert_awaited_once()
    assert command.current_option == "Stop"
    assert command._revert_deadline is None


async def test_autorevert_after_removal_is_noop(hass: HomeAssistant):
    """A revert task firing after the entity is removed must not touch the plant (#157).

    On an entry reload the auto-revert timer can fire before the queued _do_revert runs;
    acting then would stop the freshly-restored heartbeat and write state on a dead entity.
    """
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added: list = []
    await select_setup_entry(hass, entry, lambda e: added.extend(e))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    command.async_write_ha_state = MagicMock()
    command._attr_current_option = "Charge"

    # Removal (e.g. an entry reload) sets the guard flag.
    with patch.object(CoordinatorEntity, "async_will_remove_from_hass", new=AsyncMock()):
        await command.async_will_remove_from_hass()
    assert command._removed is True

    with patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await command._do_revert()

    # No effect on the plant: heartbeat untouched, no Stop written, no state write.
    mock_stop.assert_not_awaited()
    data.control.async_update_parameters.assert_not_awaited()
    command.async_write_ha_state.assert_not_called()


async def test_restored_command_reverts_when_deadline_passed(hass: HomeAssistant):
    """A forced command whose deadline passed while HA was down reverts to Stop on restore."""
    import time

    from homeassistant.core import State
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added: list = []
    await select_setup_entry(hass, entry, lambda e: added.extend(e))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    command.async_write_ha_state = MagicMock()
    command.async_get_last_state = AsyncMock(return_value=State("select.x", "Charge", {"revert_at": time.time() - 10}))

    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()),
        patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()) as mock_start,
        patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()),
    ):
        await command.async_added_to_hass()

    # Reverted, not resumed: Stop written, heartbeat never started.
    data.control.async_update_parameters.assert_awaited_with("ess-1", {"charge_discharge_command": "204"})
    mock_start.assert_not_awaited()
    assert command.current_option == "Stop"


async def test_restored_command_rearms_when_deadline_future(hass: HomeAssistant):
    """A forced command still within its window resumes dispatch and re-arms the revert."""
    import time

    from homeassistant.core import State
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added: list = []
    await select_setup_entry(hass, entry, lambda e: added.extend(e))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    future = time.time() + 300
    command.async_get_last_state = AsyncMock(return_value=State("select.x", "Charge", {"revert_at": future}))

    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()),
        patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()) as mock_start,
    ):
        await command.async_added_to_hass()

    mock_start.assert_awaited_once()  # dispatch resumed
    assert command._revert_deadline == future  # re-armed for the remaining time
    command._cancel_revert()  # avoid a lingering scheduled callback in the test


async def test_restored_stop_command_leaves_heartbeat_off(hass: HomeAssistant):
    """A restored 'Stop' command must not start the heartbeat (#112)."""
    from homeassistant.core import State
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    _setup_entry_data(entry, [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}])

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")
    command.hass = hass
    command.async_get_last_state = AsyncMock(return_value=State("select.x", "Stop"))

    with (
        patch.object(CoordinatorEntity, "async_added_to_hass", new=AsyncMock()),
        patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()) as mock_start,
    ):
        await command.async_added_to_hass()

    assert command.current_option == "Stop"
    mock_start.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dynamic devices (dispatch controls appear at runtime)
# ---------------------------------------------------------------------------


async def test_number_dynamic_add_when_device_appears(hass: HomeAssistant):
    """A dispatchable device appearing after setup gets its number entities."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [])  # no dispatchable device yet
    coordinator = data.coordinators[0]
    listeners: list = []
    coordinator.async_add_listener = lambda cb, *a: listeners.append(cb) or (lambda: None)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert added == []  # nothing dispatchable at setup

    # An ESS appears on a later poll; the coordinator notifies its listeners.
    coordinator.devices = [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    for cb in listeners:
        cb()

    assert len(added) == len(DISPATCH_NUMBERS) + 1  # + forced_dispatch_duration (#157)
    assert all(e.device_uuid == "ess-1" for e in added)

    # Firing again must not duplicate the controls.
    for cb in listeners:
        cb()
    assert len(added) == len(DISPATCH_NUMBERS) + 1  # + forced_dispatch_duration (#157)


async def test_select_dynamic_add_when_device_appears(hass: HomeAssistant):
    """A dispatchable device appearing after setup gets its select entities once."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    data = _setup_entry_data(entry, [])  # no dispatchable device yet
    coordinator = data.coordinators[0]
    listeners: list = []
    coordinator.async_add_listener = lambda cb, *a: listeners.append(cb) or (lambda: None)

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert added == []  # nothing dispatchable at setup

    # An ESS appears on a later poll; the coordinator notifies its listeners.
    coordinator.devices = [{"uuid": "ess-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    for cb in listeners:
        cb()

    assert len(added) == len(DISPATCH_SELECTS)
    assert all(e.device_uuid == "ess-1" for e in added)

    # Firing again must not duplicate the controls (unique-id guard).
    for cb in listeners:
        cb()
    assert len(added) == len(DISPATCH_SELECTS)


# ---------------------------------------------------------------------------
# Exception translations (dispatch write failures)
# ---------------------------------------------------------------------------


async def test_number_set_value_api_error_raises_translated_error(hass: HomeAssistant):
    """A failed dispatch write surfaces as a translated HomeAssistantError."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _setup_entry_data(entry, devices)
    entry_data.control.async_update_parameters = AsyncMock(side_effect=PySolarCloudException({"error": "server_busy"}))

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")

    with pytest.raises(HomeAssistantError) as exc:
        await power.async_set_native_value(2500)

    assert exc.value.translation_key == "dispatch_write_failed"
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_placeholders["param"] == "charge_discharge_power"


async def test_select_option_api_error_raises_translated_error(hass: HomeAssistant):
    """A failed dispatch command surfaces as a translated HomeAssistantError."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _setup_entry_data(entry, devices)
    entry_data.control.async_update_parameters = AsyncMock(side_effect=PySolarCloudException({"error": "server_busy"}))

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")

    with (
        patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()),
        pytest.raises(HomeAssistantError) as exc,
    ):
        await command.async_select_option("Charge")

    assert exc.value.translation_key == "dispatch_write_failed"
    assert exc.value.translation_domain == DOMAIN
    assert exc.value.translation_placeholders["param"] == "charge_discharge_command"


# ---------------------------------------------------------------------------
# Icon translations
# ---------------------------------------------------------------------------


def test_icons_json_covers_all_dispatch_entities():
    """Every dispatch number/select translation key has an icon in icons.json."""
    import json
    from pathlib import Path

    icons_path = Path(__file__).parent.parent / "custom_components" / "sungrow" / "icons.json"
    icons = json.loads(icons_path.read_text())["entity"]

    for param in DISPATCH_NUMBERS:
        assert icons["number"][param]["default"].startswith("mdi:"), f"missing icon for number.{param}"
    for param in DISPATCH_SELECTS:
        assert icons["select"][param]["default"].startswith("mdi:"), f"missing icon for select.{param}"
