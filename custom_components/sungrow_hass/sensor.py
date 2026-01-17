from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from pysolarcloud import Auth
from pysolarcloud.plants import Plants

from .const import (
    DOMAIN,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_APP_ID,
    CONF_GATEWAY,
    GATEWAYS,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Sungrow sensor based on a config entry."""
    
    # helper to get gateway URL
    gateway_key = entry.data[CONF_GATEWAY]
    host = GATEWAYS.get(gateway_key, "https://gateway.isolarcloud.eu") # Fallback to EU if mapping fails

    # Reconstruct Auth
    session = async_get_clientsession(hass)
    auth = Auth(
        host=host,
        appkey=entry.data[CONF_APP_KEY],
        access_key=entry.data[CONF_APP_SECRET],
        app_id=entry.data[CONF_APP_ID],
        websession=session,
    )
    
    # Restore tokens
    if "tokens" in entry.data:
        auth.tokens = entry.data["tokens"]
    else:
        _LOGGER.error("No tokens found in config entry")
        return

    plants_service = Plants(auth)

    # Fetch plants
    try:
        plant_list = await plants_service.async_get_plants()
    except Exception as err:
        _LOGGER.error("Failed to fetch plants: %s", err)
        return

    entities = []

    for plant_info in plant_list:
        plant_id = str(plant_info["ps_id"])
        plant_name = plant_info["ps_name"]
        
        _LOGGER.debug(f"Setting up plant: {plant_name} ({plant_id})")

        coordinator = SungrowPlantCoordinator(hass, plants_service, plant_id, plant_name)
        
        # Determine available sensors by doing a first refresh
        await coordinator.async_config_entry_first_refresh()

        if not coordinator.data:
            _LOGGER.warning(f"No data received for plant {plant_name}")
            continue

        # Create a sensor for each data point returned by the API
        # The data structure is { "P_CODE": { "code": "...", "value": ..., "unit": "...", "name": "..." } }
        for point_code, point_data in coordinator.data.items():
             entities.append(SungrowSensor(coordinator, point_code, plant_id, plant_name, point_data))

    async_add_entities(entities)


class SungrowPlantCoordinator(DataUpdateCoordinator):
    """Coordinator to manage fetching data from single plant."""

    def __init__(self, hass, plants_service, plant_id, plant_name):
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Sungrow Plant {plant_name}",
            update_interval=SCAN_INTERVAL,
        )
        self.plants_service = plants_service
        self.plant_id = plant_id

    async def _async_update_data(self):
        """Fetch data from API."""
        try:
             # async_get_realtime_data returns a dict of plants, keyed by plant_id
             # { "123": { "code1": {...}, "code2": {...} } }
             all_plants_data = await self.plants_service.async_get_realtime_data([self.plant_id])
             
             if self.plant_id in all_plants_data:
                 return all_plants_data[self.plant_id]
             return {}
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


class SungrowSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Sungrow Sensor."""

    def __init__(self, coordinator, point_code, plant_id, plant_name, init_data):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.point_code = point_code
        self.plant_id = plant_id

        # Prefer generating name from code to avoid Chinese names from API
        # The API often returns Chinese names even when locale is set to English
        if point_code.isdigit():
             sensor_name = init_data.get('name', point_code)
        else:
             sensor_name = point_code.replace("_", " ").title()

        self._attr_name = f"{plant_name} {sensor_name}"
        _LOGGER.debug(f"Created sensor: {self._attr_name} (code: {point_code})")
        self._attr_unique_id = f"{plant_id}_{point_code}"
        self._attr_icon = "mdi:solar-power-variant"
        
        # Attempt to infer device class and unit
        self._attr_native_unit_of_measurement = init_data.get("unit")
        
        # Simple inference for Power/Energy
        if self._attr_native_unit_of_measurement in ["kW", "W"]:
             self._attr_device_class = SensorDeviceClass.POWER
             self._attr_state_class = SensorStateClass.MEASUREMENT
        elif self._attr_native_unit_of_measurement in ["kWh"]:
             self._attr_device_class = SensorDeviceClass.ENERGY
             self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if self.coordinator.data and self.point_code in self.coordinator.data:
             val = self.coordinator.data[self.point_code].get("value")
             # Try convert to float if it looks like a number but is string
             try:
                 return float(val)
             except (ValueError, TypeError):
                 return val
        return None

    @property
    def extra_state_attributes(self):
        """Return attributes."""
        if self.coordinator.data and self.point_code in self.coordinator.data:
            return self.coordinator.data[self.point_code]
        return {}
