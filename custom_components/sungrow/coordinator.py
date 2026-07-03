"""Data update coordinator for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pysolarcloud import PySolarCloudException
from pysolarcloud.plants import Plants

from .auth import AUTH_ERRORS
from .const import (
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def is_auth_error(err: Exception) -> bool:
    """Return True if the error means the stored credentials are no longer valid.

    A failed token refresh raises ``PySolarCloudException`` (``TokenRefreshError``,
    error ``token_refresh_failed``), and explicit auth problems raise
    ``PySolarCloudException`` with a known error code — both matched via
    ``AUTH_ERRORS``. All require the user to re-authorize rather than waiting for a
    transient outage to clear.
    """
    if isinstance(err, PySolarCloudException):
        return err.error in AUTH_ERRORS
    return False


class SungrowPlantCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching data from a single plant."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        plants_service: Plants,
        plant_id: str,
        plant_name: str,
        devices: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the coordinator."""
        scan_seconds = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"Sungrow Plant {plant_name}",
            update_interval=timedelta(seconds=scan_seconds),
            config_entry=config_entry,
        )
        self.plants_service = plants_service
        self.plant_id = plant_id
        self.plant_name = plant_name
        self.devices: list[dict[str, Any]] = list(devices or [])
        self.extra_measure_points: dict[str, str] = dict(config_entry.options.get(CONF_EXTRA_MEASURE_POINTS, {}))
        self.enable_device_sensors: bool = bool(config_entry.options.get(CONF_ENABLE_DEVICE_SENSORS, False))
        # uuid -> { code: point } for per-device realtime (populated when enabled).
        self.device_data: dict[str, dict[str, Any]] = {}
        # Whether the dispatch device accepts parameter writes. Checked once at
        # setup; defaults True (fail-open) so an unavailable/unknown check never
        # hides working controls.
        self.dispatch_update_supported: bool = True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the API for this plant."""
        try:
            # async_get_realtime_data returns a dict of plants keyed by plant_id:
            # { "123": { "code1": {...}, "code2": {...} } }
            all_plants_data = await self.plants_service.async_get_realtime_data(
                [self.plant_id], extra_measure_points=self.extra_measure_points or None
            )
        except Exception as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(f"Authentication with iSolarCloud failed: {err}") from err
            raise UpdateFailed(f"Error communicating with iSolarCloud API: {err}") from err

        # Refresh the device list so devices added to the plant after setup are
        # picked up at runtime (dynamic-devices) and removed ones can be pruned
        # (stale-devices). Best-effort: keep the previous list on failure.
        await self._async_refresh_devices()

        if self.enable_device_sensors:
            self.device_data = await self._async_fetch_device_data()

        # pysolarcloud is untyped, so the realtime payload is Any.
        return cast("dict[str, Any]", all_plants_data.get(self.plant_id, {}))

    async def _async_refresh_devices(self) -> None:
        """Re-fetch the plant's device list (best effort, non-fatal)."""
        try:
            devices = await self.plants_service.async_get_plant_devices(self.plant_id)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not refresh devices for plant %s: %s", self.plant_id, err)
            return
        # Mutate in place so holders of this list (runtime_data.devices) see updates.
        self.devices[:] = list(devices or [])

    async def _async_fetch_device_data(self) -> dict[str, dict[str, Any]]:
        """Fetch per-device realtime for each distinct device type (best effort).

        The plant realtime endpoint only returns the plant-level points, so devices
        like EV chargers or meters need a per-device fetch (issue #74). This is
        best-effort: a device type whose endpoint is unavailable or errors simply
        contributes nothing rather than failing the whole update. Any user-configured
        extra measure points are requested here too, so newly identified charger/meter
        point IDs surface without a code change.
        """
        merged: dict[str, dict[str, Any]] = {}
        seen_types: set[Any] = set()
        for device in self.devices:
            device_type = device.get("device_type")
            if device_type is None:
                continue
            type_id = getattr(device_type, "value", device_type)
            if type_id in seen_types:
                continue
            seen_types.add(type_id)
            try:
                result = await self.plants_service.async_get_device_realtime(
                    self.plant_id,
                    device_type,
                    extra_measure_points=self.extra_measure_points or None,
                )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("Per-device realtime failed for plant %s type %s: %s", self.plant_id, type_id, err)
                continue
            for uuid, points in (result or {}).items():
                merged.setdefault(str(uuid), {}).update(points)
        return merged
