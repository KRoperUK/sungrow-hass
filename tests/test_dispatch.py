"""Tests for the Sungrow dispatch (number/select) platforms."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


def _entry_data_for(devices):
    control = MagicMock()
    control.async_update_parameters = AsyncMock(return_value=[])
    control.async_heartbeat = AsyncMock()
    control.heartbeat_loop = AsyncMock()
    return {
        "coordinators": [_coordinator_with("12345", "Test Plant")],
        "control": control,
        "devices": {"12345": devices},
        "heartbeat_stop": {},
    }


async def test_number_setup_creates_entities_for_ess_device(hass: HomeAssistant):
    """Dispatch numbers are created when an ESS device is discovered."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM", "device_name": "Inverter 1"}]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _entry_data_for(devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == len(DISPATCH_NUMBERS)
    power = next(e for e in added if e.param == "charge_discharge_power")
    assert power._attr_device_class == NumberDeviceClass.POWER
    assert power._attr_native_unit_of_measurement == "W"


async def test_number_setup_falls_back_to_inverter(hass: HomeAssistant):
    """Dispatch numbers fall back to an inverter device if no ESS device exists."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-2", "device_type": "INVERTER"}]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _entry_data_for(devices)

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == len(DISPATCH_NUMBERS)


async def test_number_setup_no_devices(hass: HomeAssistant):
    """No dispatch numbers are created when no dispatchable devices are found."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _entry_data_for([])

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []


async def test_number_set_value_calls_control(hass: HomeAssistant):
    """Setting a number entity writes the parameter via Control."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _entry_data_for(devices)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")

    with patch("custom_components.sungrow.number.async_start_heartbeat", new=AsyncMock()):
        await power.async_set_native_value(2500)

    entry_data["control"].async_update_parameters.assert_awaited_once_with(
        "dev-uuid-1", {"charge_discharge_power": "2500"}
    )


async def test_number_starts_heartbeat_for_power_changes(hass: HomeAssistant):
    """Changing charge/discharge power starts the EMS heartbeat loop."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _entry_data_for(devices)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    power = next(e for e in added if e.param == "charge_discharge_power")

    with patch("custom_components.sungrow.number.async_start_heartbeat", new=AsyncMock()) as mock_start:
        await power.async_set_native_value(1500)

    mock_start.assert_awaited_once()
    assert mock_start.call_args.args[3] == "dev-uuid-1"


async def test_select_setup_creates_entities_for_ess_device(hass: HomeAssistant):
    """Dispatch selects are created when an ESS device is discovered."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _entry_data_for(devices)

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
    entry_data = _entry_data_for(devices)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")

    with patch("custom_components.sungrow.select.async_start_heartbeat", new=AsyncMock()):
        await command.async_select_option("Charge")

    entry_data["control"].async_update_parameters.assert_awaited_once_with(
        "dev-uuid-1", {"charge_discharge_command": "170"}
    )


async def test_select_stop_stops_heartbeat(hass: HomeAssistant):
    """Selecting 'Stop' stops the EMS heartbeat loop."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _entry_data_for(devices)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    added = []
    await select_setup_entry(hass, entry, lambda entities: added.extend(entities))
    command = next(e for e in added if e.param == "charge_discharge_command")

    command.hass = hass
    with patch("custom_components.sungrow.select.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await command.async_select_option("Stop")

    mock_stop.assert_awaited_once_with(hass, command.coordinator.config_entry, "12345")


async def test_entity_removal_stops_heartbeat(hass: HomeAssistant):
    """Removing a dispatch entity stops the heartbeat for the plant."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM"}]
    entry_data = _entry_data_for(devices)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry_data

    added = []
    await number_setup_entry(hass, entry, lambda entities: added.extend(entities))
    number = added[0]

    number.hass = hass
    with patch("custom_components.sungrow.number.async_stop_heartbeat", new=AsyncMock()) as mock_stop:
        await number.async_will_remove_from_hass()

    mock_stop.assert_awaited_once_with(hass, number.coordinator.config_entry, "12345")
