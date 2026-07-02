"""Tests for the Sungrow dispatch (number/select) platforms."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from pysolarcloud.plants import DeviceType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import SungrowData, select_dispatch_device
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
    return coordinator


def _setup_entry_data(entry, devices) -> SungrowData:
    """Build a SungrowData and attach it to the entry as runtime_data."""
    control = MagicMock()
    control.async_update_parameters = AsyncMock(return_value=[])
    control.async_heartbeat = AsyncMock()
    control.heartbeat_loop = AsyncMock()
    data = SungrowData(
        coordinators=[_coordinator_with("12345", "Test Plant")],
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

    assert len(added) == len(DISPATCH_NUMBERS)
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

    assert len(added) == len(DISPATCH_NUMBERS)


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

    with patch("custom_components.sungrow.number.async_start_heartbeat", new=AsyncMock()):
        await power.async_set_native_value(2500)

    entry_data.control.async_update_parameters.assert_awaited_once_with(
        "dev-uuid-1", {"charge_discharge_power": "2500"}
    )


async def test_number_starts_heartbeat_for_power_changes(hass: HomeAssistant):
    """Changing charge/discharge power starts the EMS heartbeat loop."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")

    with patch("custom_components.sungrow.number.async_start_heartbeat", new=AsyncMock()) as mock_start:
        await power.async_set_native_value(1500)

    mock_start.assert_awaited_once()
    assert mock_start.call_args.args[3] == "dev-uuid-1"


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


async def test_select_removal_stops_heartbeat(hass: HomeAssistant):
    """Removing a select entity stops the heartbeat for the plant."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = added[0]

    command.hass = hass
    with patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await command.async_will_remove_from_hass()

    mock_stop.assert_awaited_once_with(hass, command.coordinator.config_entry, "12345")


async def test_entity_removal_stops_heartbeat(hass: HomeAssistant):
    """Removing a dispatch entity stops the heartbeat for the plant."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    _setup_entry_data(entry, devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    number = added[0]

    number.hass = hass
    with patch("custom_components.sungrow.number.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await number.async_will_remove_from_hass()

    mock_stop.assert_awaited_once_with(hass, number.coordinator.config_entry, "12345")
