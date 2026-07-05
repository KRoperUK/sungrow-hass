"""Tests for the Sungrow binary_sensor platform (device fault, #151)."""

from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pysolarcloud.plants import DeviceFaultStaus
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import SungrowData
from custom_components.sungrow.binary_sensor import (
    SungrowDeviceFaultBinarySensor,
    async_setup_entry,
    fault_is_on,
)
from custom_components.sungrow.const import DOMAIN

from .conftest import MOCK_CONFIG_DATA


def _coordinator_with(devices):
    coordinator = MagicMock()
    coordinator.plant_id = "12345"
    coordinator.plant_name = "Test Plant"
    coordinator.config_entry = MagicMock()
    coordinator.last_update_success = True
    coordinator.devices = devices
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

    assert len(added) == 2
    by_uuid = {e.device_uuid: e for e in added}
    assert by_uuid["inv-1"]._attr_device_class == BinarySensorDeviceClass.PROBLEM
    assert by_uuid["inv-1"]._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert by_uuid["inv-1"]._attr_unique_id == "12345_inv-1_fault"
    assert by_uuid["inv-1"].is_on is False
    assert by_uuid["meter-1"].is_on is True
    assert by_uuid["meter-1"].extra_state_attributes["fault_status"] == "FAULT"


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
