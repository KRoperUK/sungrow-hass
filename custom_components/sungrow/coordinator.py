"""Data update coordinator for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
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
    DEVICE_REFRESH_INTERVAL,
)

# Upper bound on a single poll's cloud calls, so a hung request can neither stall
# the coordinator indefinitely nor let successive polls pile up.
MAX_POLL_TIMEOUT = 60

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


# Actionable hints for otherwise-opaque iSolarCloud result codes. Both are Developer-
# Portal whitelist rejections (Appendix 2) that keep retrying (see AUTH_ERRORS), so the
# raw code would just repeat in the log with no explanation of how to fix it.
API_ERROR_HINTS = {
    "E918": (
        "iSolarCloud rejected the request: this client's IP address is not in your API "
        "application's IP whitelist (E918). In the iSolarCloud Developer Portal, add this "
        "machine's public IP to the whitelist or disable it, then it recovers automatically."
    ),
    "E919": (
        "iSolarCloud rejected the request: your account is not in your API application's user "
        "whitelist (E919). In the iSolarCloud Developer Portal, add your account to the whitelist "
        "or disable it, then it recovers automatically."
    ),
}


def describe_api_error(err: Exception) -> str | None:
    """Return an actionable message for a known iSolarCloud error code, else None."""
    if isinstance(err, PySolarCloudException) and err.error is not None:
        return API_ERROR_HINTS.get(err.error)
    return None


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
        # Cap each poll's requests at the scan interval, never longer than 60 s.
        self._poll_timeout: float = min(scan_seconds, MAX_POLL_TIMEOUT)
        # Monotonic timestamp of the last device-list refresh; None until first poll.
        self._last_device_refresh: float | None = None
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
            async with asyncio.timeout(self._poll_timeout):
                all_plants_data = await self.plants_service.async_get_realtime_data(
                    [self.plant_id], extra_measure_points=self.extra_measure_points or None
                )
        except Exception as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(f"Authentication with iSolarCloud failed: {err}") from err
            # A timeout arrives here as TimeoutError and is treated as transient.
            raise UpdateFailed(describe_api_error(err) or f"Error communicating with iSolarCloud API: {err}") from err

        # Refresh the device list so devices added to the plant after setup are
        # picked up at runtime (dynamic-devices) and removed ones can be pruned
        # (stale-devices). Throttled and best-effort: keep the previous list on failure.
        await self._async_maybe_refresh_devices()

        if self.enable_device_sensors:
            self.device_data = await self._async_fetch_device_data()

        # pysolarcloud is untyped, so the realtime payload is Any.
        return cast("dict[str, Any]", all_plants_data.get(self.plant_id, {}))

    async def _async_maybe_refresh_devices(self) -> None:
        """Refresh the device list periodically rather than on every poll (saves quota).

        The plant's device set changes rarely, so re-listing it every realtime poll
        wastes calls against the ~2000/hour free-plan cap. Refresh on the first poll
        and thereafter only once ``DEVICE_REFRESH_INTERVAL`` has elapsed.
        """
        now = self.hass.loop.time()
        if self._last_device_refresh is not None and (now - self._last_device_refresh) < DEVICE_REFRESH_INTERVAL:
            return
        self._last_device_refresh = now
        await self._async_refresh_devices()

    async def _async_refresh_devices(self) -> None:
        """Re-fetch the plant's device list (best effort, non-fatal)."""
        try:
            async with asyncio.timeout(self._poll_timeout):
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
                async with asyncio.timeout(self._poll_timeout):
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
