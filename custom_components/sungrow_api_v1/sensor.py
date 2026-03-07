"""Sensor platform for Sungrow iSolarCloud integration (API v1)."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    GATEWAYS,
)
from .isolarcloud_api import ISolarCloudAPI, ISolarCloudError

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow sensor based on a config entry."""
    gateway_key = entry.data[CONF_GATEWAY]
    host = GATEWAYS.get(gateway_key, "https://gateway.isolarcloud.eu")

    session = async_get_clientsession(hass)
    api = ISolarCloudAPI(
        host=host,
        appkey=entry.data[CONF_APP_KEY],
        access_key=entry.data[CONF_APP_SECRET],
        user_account=entry.data[CONF_USERNAME],
        user_password=entry.data[CONF_PASSWORD],
        websession=session,
    )

    # Restore token if available, otherwise login
    if "token" in entry.data:
        api.token = entry.data["token"]
        api.user_id = entry.data.get("user_id")
    else:
        try:
            await api.async_login()
        except Exception as err:
            _LOGGER.error("Failed to login to iSolarCloud: %s", err)
            return

    # Fetch plants
    try:
        plant_list = await api.async_get_plant_list()
    except ISolarCloudError:
        # Token may be expired, try re-login
        _LOGGER.info("Token may be expired, attempting re-login")
        try:
            await api.async_login()
            plant_list = await api.async_get_plant_list()
        except Exception as err:
            _LOGGER.error("Failed to fetch plants: %s", err)
            return
    except Exception as err:
        _LOGGER.error("Failed to fetch plants: %s", err)
        return

    entities = []

    for plant_info in plant_list:
        plant_id = str(plant_info.get("ps_id", plant_info.get("id", "")))
        plant_name = plant_info.get("ps_name", plant_info.get("name", f"Plant {plant_id}"))

        if not plant_id:
            continue

        _LOGGER.debug("Setting up plant: %s (%s)", plant_name, plant_id)

        coordinator = SungrowPlantCoordinator(hass, entry, api, plant_id, plant_name)

        await coordinator.async_config_entry_first_refresh()

        if not coordinator.data:
            _LOGGER.warning("No data received for plant %s", plant_name)
            continue

        for point_code, point_data in coordinator.data.items():
            entities.append(SungrowSensor(coordinator, point_code, plant_id, plant_name, point_data, entry.entry_id))

    async_add_entities(entities)


class SungrowPlantCoordinator(DataUpdateCoordinator):
    """Coordinator to manage fetching data from single plant."""

    def __init__(self, hass, config_entry, api: ISolarCloudAPI, plant_id, plant_name):
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"Sungrow Plant {plant_name}",
            update_interval=SCAN_INTERVAL,
        )
        self.config_entry = config_entry
        self.api = api
        self.plant_id = plant_id

    async def _async_update_data(self):
        """Fetch data from API."""
        try:
            return await self.api.async_get_plant_realtime_data(self.plant_id)
        except ISolarCloudError as err:
            # Try re-login on auth errors
            try:
                await self.api.async_login()
                return await self.api.async_get_plant_realtime_data(self.plant_id)
            except Exception as login_err:
                raise UpdateFailed(f"Error communicating with API: {err}") from login_err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err


class SungrowSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Sungrow Sensor."""

    has_entity_name = True

    def __init__(self, coordinator, point_code, plant_id, plant_name, init_data, entry_id):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.point_code = point_code
        self.plant_id = plant_id

        if point_code.isdigit():
            sensor_name = init_data.get("name", f"Sensor {point_code}")
        else:
            sensor_name = point_code.replace("_", " ").title()

        self._attr_name = sensor_name
        self._attr_unique_id = f"{plant_id}_{point_code}"
        self._attr_icon = "mdi:solar-power-variant"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant_id)},
            name=plant_name,
            manufacturer="Sungrow",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://isolarcloud.eu",
        )

        initial_value = init_data.get("value")
        if initial_value is None or str(initial_value).strip() == "" or str(initial_value).lower() == "unknown":
            self._attr_entity_registry_enabled_default = False

        self._attr_native_unit_of_measurement = init_data.get("unit")

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
