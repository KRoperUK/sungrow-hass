"""Data update coordinator for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pysolarcloud import PySolarCloudException
from pysolarcloud.plants import Plants

from .auth import AUTH_ERRORS
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


def is_auth_error(err: Exception) -> bool:
    """Return True if the error means the stored credentials are no longer valid.

    A failed token refresh surfaces as a ``KeyError`` from pysolarcloud (the refresh
    response has no ``access_token``), while explicit auth problems raise
    ``PySolarCloudException`` with a known error code. Both require the user to
    re-authorize rather than waiting for a transient outage to clear.
    """
    if isinstance(err, KeyError):
        return True
    return isinstance(err, PySolarCloudException) and err.error in AUTH_ERRORS


class SungrowPlantCoordinator(DataUpdateCoordinator):
    """Coordinator to manage fetching data from a single plant."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        plants_service: Plants,
        plant_id: str,
        plant_name: str,
    ) -> None:
        """Initialize the coordinator."""
        scan_minutes = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"Sungrow Plant {plant_name}",
            update_interval=timedelta(minutes=scan_minutes),
            config_entry=config_entry,
        )
        self.plants_service = plants_service
        self.plant_id = plant_id
        self.plant_name = plant_name

    async def _async_update_data(self):
        """Fetch data from the API for this plant."""
        try:
            # async_get_realtime_data returns a dict of plants keyed by plant_id:
            # { "123": { "code1": {...}, "code2": {...} } }
            all_plants_data = await self.plants_service.async_get_realtime_data([self.plant_id])
        except Exception as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(f"Authentication with iSolarCloud failed: {err}") from err
            raise UpdateFailed(f"Error communicating with iSolarCloud API: {err}") from err

        return all_plants_data.get(self.plant_id, {})
