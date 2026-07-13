"""Tests for the Sungrow binary_sensor platform (device fault, #151)."""

from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pysolarcloud.plants import DeviceFaultStaus, DeviceType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import SungrowData
from custom_components.sungrow.binary_sensor import (
    SungrowDeviceConnectivityBinarySensor,
    SungrowDeviceFaultBinarySensor,
    SungrowModbusConnectivityBinarySensor,
    async_setup_entry,
    connectivity_is_on,
    fault_is_on,
)
from custom_components.sungrow.const import DOMAIN

from .conftest import MOCK_CONFIG_DATA


def _coordinator_with(devices, device_data=None):
    coordinator = MagicMock()
    coordinator.plant_id = "12345"
    coordinator.plant_name = "Test Plant"
    coordinator.config_entry = MagicMock()
    coordinator.last_update_success = True
    coordinator.devices = devices
    coordinator.device_data = device_data or {}
    return coordinator


def _setup_entry_data(entry, devices) -> SungrowData:
    coordinator = _coordinator_with(devices)
    data = SungrowData(coordinators=[coordinator], control=MagicMock(), devices={"12345": devices})
    entry.runtime_data = data
    return data


def test_fault_is_on_maps_all_status_forms():
    """fault_is_on handles the enum, int and string representations of the status."""
    assert fault_is_on(DeviceFaultStaus.NORMAL) is False
    assert fault_is_on(DeviceFaultStaus.FAULT) is True
    assert fault_is_on(DeviceFaultStaus.ALARM) is True
    assert fault_is_on("NORMAL") is False
    assert fault_is_on("FAULT") is True
    assert fault_is_on(4) is False
    assert fault_is_on(1) is True
    # Unknown / missing -> None (unknown), never a misleading "no problem".
    assert fault_is_on(None) is None
    assert fault_is_on("mystery") is None


async def test_fault_binary_sensor_created_per_device(hass: HomeAssistant):
    """A PROBLEM binary sensor is created for every device with a uuid."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [
        {"uuid": "inv-1", "device_name": "Inverter1", "dev_fault_status": DeviceFaultStaus.NORMAL},
        {"uuid": "meter-1", "device_name": "Meter1", "dev_fault_status": DeviceFaultStaus.FAULT},
        {"device_name": "no-uuid"},  # skipped
    ]
    _setup_entry_data(entry, devices)

    added: list = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    fault = {e.device_uuid: e for e in added if isinstance(e, SungrowDeviceFaultBinarySensor)}
    assert set(fault) == {"inv-1", "meter-1"}  # the no-uuid device is skipped
    assert fault["inv-1"]._attr_device_class == BinarySensorDeviceClass.PROBLEM
    assert fault["inv-1"]._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert fault["inv-1"]._attr_unique_id == "12345_inv-1_fault"
    assert fault["inv-1"].is_on is False
    assert fault["meter-1"].is_on is True
    assert fault["meter-1"].extra_state_attributes["fault_status"] == "FAULT"


def test_fault_sensor_surfaces_operating_status_reason():
    """The Fault sensor exposes a human-readable operating-status reason (#182)."""
    devices = [{"uuid": "inv-1", "device_name": "Inverter1", "dev_fault_status": DeviceFaultStaus.FAULT}]
    device_data = {"inv-1": {"operating_status": {"id": "29", "code": "operating_status", "value": "21760"}}}
    coordinator = _coordinator_with(devices, device_data=device_data)
    sensor = SungrowDeviceFaultBinarySensor(coordinator, devices[0])
    attrs = sensor.extra_state_attributes
    assert attrs["fault_status"] == "FAULT"
    assert attrs["operating_status"] == "Shut down due to faults"


def test_fault_sensor_operating_status_reason_for_ess():
    """ESS/hybrid operating status resolves via point 13146, not the inverter point (#182)."""
    devices = [{"uuid": "ess-1", "device_name": "Hybrid", "dev_fault_status": DeviceFaultStaus.ALARM}]
    device_data = {"ess-1": {"operating_status": {"id": "13146", "code": "operating_status", "value": "37120"}}}
    coordinator = _coordinator_with(devices, device_data=device_data)
    sensor = SungrowDeviceFaultBinarySensor(coordinator, devices[0])
    assert sensor.extra_state_attributes["operating_status"] == "Running with alarm"


def test_fault_sensor_operating_status_none_when_not_reported():
    """Devices with no operating-status reading expose operating_status=None (#182)."""
    devices = [{"uuid": "meter-1", "device_name": "Meter1", "dev_fault_status": DeviceFaultStaus.NORMAL}]
    coordinator = _coordinator_with(devices)  # empty device_data
    sensor = SungrowDeviceFaultBinarySensor(coordinator, devices[0])
    assert sensor.extra_state_attributes["operating_status"] is None


async def test_fault_binary_sensor_unavailable_when_device_gone(hass: HomeAssistant):
    """The sensor goes unavailable if its device drops out of the plant on a later refresh."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [{"uuid": "inv-1", "device_name": "Inverter1", "dev_fault_status": DeviceFaultStaus.NORMAL}]
    data = _setup_entry_data(entry, devices)

    added: list = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
    sensor = added[0]
    assert sensor.available is True

    data.coordinators[0].devices = []
    assert sensor.available is False
    assert sensor.is_on is None


def test_connectivity_is_on_maps_dev_status():
    """connectivity_is_on maps dev_status 1/0 to online/offline, else unknown."""
    assert connectivity_is_on("1") is True
    assert connectivity_is_on(1) is True
    assert connectivity_is_on("0") is False
    assert connectivity_is_on(0) is False
    assert connectivity_is_on(None) is None
    assert connectivity_is_on("x") is None


async def test_connectivity_binary_sensor(hass: HomeAssistant):
    """A CONNECTIVITY sensor per device reflects dev_status + exposes the commissioning date."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [
        {"uuid": "meter-1", "device_name": "Meter1", "dev_status": "0", "grid_connection_date": "2025-10-26 23:41:51"}
    ]
    _setup_entry_data(entry, devices)

    added: list = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    conn = next(e for e in added if isinstance(e, SungrowDeviceConnectivityBinarySensor))
    assert conn._attr_device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert conn._attr_unique_id == "12345_meter-1_online"
    assert conn.is_on is False  # dev_status "0" -> offline
    assert conn.extra_state_attributes["commissioning_date"] == "2025-10-26 23:41:51"


def test_fault_binary_sensor_device_info_enriched():
    """The binary sensor's device card carries model/serial (via build_device_info)."""
    coordinator = _coordinator_with(
        [
            {
                "uuid": "inv-1",
                "device_name": "Inv",
                "device_model_code": "SG3.6RS",
                "device_sn": "A1",
                "factory_name": "SUNGROW",
            }
        ]
    )
    sensor = SungrowDeviceFaultBinarySensor(coordinator, coordinator.devices[0])
    info = sensor._attr_device_info
    assert info["model"] == "SG3.6RS"
    assert info["serial_number"] == "A1"
    assert (DOMAIN, "inv-1") in info["identifiers"]


async def test_modbus_connectivity_binary_sensor(hass: HomeAssistant):
    """A Modbus-only entry creates a connectivity sensor driven by last_update_success."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    devices = [
        {
            "uuid": "inv-1",
            "device_name": "Inverter1",
            "device_type": DeviceType.INVERTER,
            "device_model_code": "SG3.6RS",
            "device_sn": "A1",
            "factory_name": "SUNGROW",
        }
    ]
    coordinator = _coordinator_with(devices)
    coordinator.plants_service = None  # Modbus-only
    coordinator.via_plant_id = None
    coordinator.local_configuration_url = "http://10.0.0.9"
    coordinator.modbus_diagnostics = {
        "device_family": "sg_rs",
        "skipped_blocks": [{"start": 13035, "count": 12}],
        "last_error": None,
    }
    data = SungrowData(coordinators=[coordinator], control=None, devices={"12345": devices})
    entry.runtime_data = data

    added: list = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    conn = next((e for e in added if isinstance(e, SungrowModbusConnectivityBinarySensor)), None)
    assert conn is not None
    assert conn._attr_device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert conn._attr_unique_id == "12345_inv-1_online"
    assert conn.is_on is True  # last_update_success = True
    attrs = conn.extra_state_attributes
    assert attrs["device_family"] == "sg_rs"
    assert attrs["skipped_blocks"] == [{"start": 13035, "count": 12}]
    assert "last_error" not in attrs  # None values are not exposed

    # Toggle last_update_success
    coordinator.last_update_success = False
    assert conn.is_on is False
