"""Tests for the Sungrow sensor platform."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.sensor import (
    SungrowPlantCoordinator,
    SungrowSensor,
    async_setup_entry,
)

from .conftest import MOCK_CONFIG_DATA, MOCK_REALTIME_DATA


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession to prevent background thread creation."""
    with patch(
        "custom_components.sungrow.sensor.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# SungrowSensor unit tests
# ---------------------------------------------------------------------------


class TestSungrowSensor:
    """Unit tests for SungrowSensor entity."""

    def _make_coordinator(self, data=None):
        """Create a minimal mock coordinator."""
        coordinator = MagicMock()
        coordinator.data = data or {}
        return coordinator

    def test_sensor_name_from_code(self):
        """Test sensor name is derived from the point code."""
        coordinator = self._make_coordinator()
        init_data = {"code": "total_active_power", "value": "5.0", "unit": "kW", "name": "Total"}
        sensor = SungrowSensor(coordinator, "total_active_power", "123", "My Plant", init_data, "test_entry")

        assert sensor._attr_name == "Total Active Power"
        assert sensor._attr_unique_id == "123_total_active_power"

    def test_sensor_name_numeric_code_fallback(self):
        """Test sensor with a numeric code falls back to init_data name."""
        coordinator = self._make_coordinator()
        init_data = {"code": "12345", "value": "99", "unit": "W", "name": "Some Sensor"}
        sensor = SungrowSensor(coordinator, "12345", "123", "My Plant", init_data, "test_entry")

        assert sensor._attr_name == "Some Sensor"

    def test_sensor_device_class_power_kw(self):
        """Test kW unit infers POWER device class."""
        coordinator = self._make_coordinator()
        init_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", init_data, "test_entry")

        assert sensor._attr_device_class == SensorDeviceClass.POWER
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_sensor_device_class_power_w(self):
        """Test W unit infers POWER device class."""
        coordinator = self._make_coordinator()
        init_data = {"code": "power", "value": "5000", "unit": "W", "name": "Power"}
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", init_data, "test_entry")

        assert sensor._attr_device_class == SensorDeviceClass.POWER
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT

    def test_sensor_device_class_energy_kwh(self):
        """Test kWh unit infers ENERGY device class."""
        coordinator = self._make_coordinator()
        init_data = {"code": "energy", "value": "12.0", "unit": "kWh", "name": "Energy"}
        sensor = SungrowSensor(coordinator, "energy", "123", "Plant", init_data, "test_entry")

        assert sensor._attr_device_class == SensorDeviceClass.ENERGY
        assert sensor._attr_state_class == SensorStateClass.TOTAL_INCREASING

    def test_sensor_device_class_unknown_unit(self):
        """Test unknown unit doesn't set device class."""
        coordinator = self._make_coordinator()
        init_data = {"code": "status", "value": "OK", "unit": "", "name": "Status"}
        sensor = SungrowSensor(coordinator, "status", "123", "Plant", init_data, "test_entry")

        assert not hasattr(sensor, "_attr_device_class") or sensor._attr_device_class is None

    def test_sensor_icon(self):
        """Test all sensors use the solar icon."""
        coordinator = self._make_coordinator()
        init_data = {"code": "x", "value": "1", "unit": "", "name": "X"}
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", init_data, "test_entry")

        assert sensor._attr_icon == "mdi:solar-power-variant"

    def test_sensor_device_info(self):
        """Test sensor has device_info grouping it under its plant."""
        coordinator = self._make_coordinator()
        init_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        sensor = SungrowSensor(coordinator, "power", "456", "My Solar Plant", init_data, "test_entry")

        assert sensor._attr_device_info is not None
        assert sensor._attr_device_info["identifiers"] == {("sungrow", "456")}
        assert sensor._attr_device_info["name"] == "My Solar Plant"
        assert sensor._attr_device_info["manufacturer"] == "Sungrow"

    def test_sensor_disabled_by_default(self):
        """Test sensors with no value are disabled by default."""
        coordinator = self._make_coordinator()

        # Test None
        init_none = {"code": "x", "value": None, "unit": "", "name": "X"}
        s1 = SungrowSensor(coordinator, "x", "123", "Plant", init_none, "entry")
        assert s1.entity_registry_enabled_default is False

        # Test empty string
        init_empty = {"code": "y", "value": "  ", "unit": "", "name": "Y"}
        s2 = SungrowSensor(coordinator, "y", "123", "Plant", init_empty, "entry")
        assert s2.entity_registry_enabled_default is False

        # Test "Unknown" literal
        init_unk = {"code": "z", "value": "Unknown", "unit": "", "name": "Z"}
        s3 = SungrowSensor(coordinator, "z", "123", "Plant", init_unk, "entry")
        assert s3.entity_registry_enabled_default is False

        # Test valid value is NOT disabled
        init_val = {"code": "v", "value": "1.2", "unit": "", "name": "V"}
        s4 = SungrowSensor(coordinator, "v", "123", "Plant", init_val, "entry")
        assert s4.entity_registry_enabled_default is True

    def test_native_value_float_conversion(self):
        """Test native_value converts string numbers to float."""
        data = {"power": {"code": "power", "value": "5.23", "unit": "kW", "name": "Power"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", data["power"], "test_entry")

        assert sensor.native_value == 5.23

    def test_native_value_non_numeric(self):
        """Test native_value returns raw string for non-numeric values."""
        data = {"status": {"code": "status", "value": "Running", "unit": "", "name": "Status"}}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "status", "123", "Plant", data["status"], "test_entry")

        assert sensor.native_value == "Running"

    def test_native_value_none_when_missing(self):
        """Test native_value returns None when data is missing."""
        coordinator = self._make_coordinator({})
        init_data = {"code": "missing", "value": "0", "unit": "", "name": "Missing"}
        sensor = SungrowSensor(coordinator, "missing", "123", "Plant", init_data, "test_entry")

        assert sensor.native_value is None

    def test_native_value_none_when_coordinator_data_none(self):
        """Test native_value returns None when coordinator.data is None."""
        coordinator = self._make_coordinator(None)
        init_data = {"code": "x", "value": "0", "unit": "", "name": "X"}
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", init_data, "test_entry")

        assert sensor.native_value is None

    def test_extra_state_attributes(self):
        """Test extra_state_attributes returns the full data point dict."""
        point_data = {"code": "power", "value": "5.0", "unit": "kW", "name": "Power"}
        data = {"power": point_data}
        coordinator = self._make_coordinator(data)
        sensor = SungrowSensor(coordinator, "power", "123", "Plant", point_data, "test_entry")

        assert sensor.extra_state_attributes == point_data

    def test_extra_state_attributes_empty_when_missing(self):
        """Test extra_state_attributes returns {} when data is missing."""
        coordinator = self._make_coordinator({})
        init_data = {"code": "x", "value": "0", "unit": "", "name": "X"}
        sensor = SungrowSensor(coordinator, "x", "123", "Plant", init_data, "test_entry")

        assert sensor.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# SungrowPlantCoordinator unit tests
# ---------------------------------------------------------------------------


class TestSungrowPlantCoordinator:
    """Unit tests for the data update coordinator."""

    async def test_update_data_success(self, hass: HomeAssistant):
        """Test successful data fetch returns plant data."""
        mock_plants = MagicMock()
        mock_plants.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)

        coordinator = SungrowPlantCoordinator(hass, mock_plants, "12345", "Test Plant")
        data = await coordinator._async_update_data()

        assert "total_active_power" in data
        assert data["total_active_power"]["value"] == "5.23"

    async def test_update_data_missing_plant(self, hass: HomeAssistant):
        """Test returns empty dict when plant_id is not in response."""
        mock_plants = MagicMock()
        mock_plants.async_get_realtime_data = AsyncMock(return_value={"99999": {}})

        coordinator = SungrowPlantCoordinator(hass, mock_plants, "12345", "Test Plant")
        data = await coordinator._async_update_data()

        assert data == {}

    async def test_update_data_api_error(self, hass: HomeAssistant):
        """Test API error raises UpdateFailed."""
        mock_plants = MagicMock()
        mock_plants.async_get_realtime_data = AsyncMock(side_effect=Exception("API down"))

        coordinator = SungrowPlantCoordinator(hass, mock_plants, "12345", "Test Plant")

        with pytest.raises(UpdateFailed, match="Error communicating with API"):
            await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# async_setup_entry integration test
# ---------------------------------------------------------------------------


async def test_sensor_setup_creates_entities(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test async_setup_entry creates sensors for each data point."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    added_entities = []

    def capture_entities(entities):
        added_entities.extend(entities)

    await async_setup_entry(hass, entry, capture_entities)

    # Plant 12345 has 3 data points, plant 67890 has 1
    assert len(added_entities) == 4

    # Check that we have the expected sensors
    names = [e._attr_name for e in added_entities]
    assert "Total Active Power" in names
    assert "Daily Energy" in names
    assert "Device Status" in names
    # Second plant also has Total Active Power — check we have 2
    assert names.count("Total Active Power") == 2


async def test_sensor_setup_no_tokens(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test async_setup_entry returns early when no tokens in config."""
    data = MOCK_CONFIG_DATA.copy()
    del data["tokens"]
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    added_entities = []
    await async_setup_entry(hass, entry, lambda entities: added_entities.extend(entities))

    assert len(added_entities) == 0


async def test_sensor_setup_plant_fetch_fails(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test async_setup_entry handles plant fetch failure gracefully."""
    mock_plants_service.async_get_plants = AsyncMock(side_effect=Exception("Network error"))

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    added_entities = []
    await async_setup_entry(hass, entry, lambda entities: added_entities.extend(entities))

    assert len(added_entities) == 0


async def test_sensor_setup_skips_plant_with_no_data(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test that plants returning empty data are skipped without creating entities."""
    # Return empty data for all plants — covers the `if not coordinator.data: continue` branch
    mock_plants_service.async_get_realtime_data = AsyncMock(
        return_value={
            "12345": {},
            "67890": {},
        }
    )

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    added_entities = []
    await async_setup_entry(hass, entry, lambda entities: added_entities.extend(entities))

    # Both plants had empty data, so no sensors should be created
    assert len(added_entities) == 0
