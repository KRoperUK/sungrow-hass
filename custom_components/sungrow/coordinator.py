"""Data update coordinator for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pysolarcloud import AuthError, PySolarCloudException
from pysolarcloud.plants import DeviceType, Plants

from .auth import AUTH_ERRORS
from .const import (
    BATTERY_DEVICE_POINTS,
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_REFRESH_INTERVAL,
    DOMAIN,
    INVERTER_DIAGNOSTIC_POINTS,
)

# Upper bound on a single poll's cloud calls, so a hung request can neither stall
# the coordinator indefinitely nor let successive polls pile up.
MAX_POLL_TIMEOUT = 60

# How long (seconds) to keep serving the last-good data — staying "available" — when
# polls fail transiently, before marking entities unavailable. Rides out the
# intermittent cloud/device hiccups that would otherwise flap every entity several
# times a minute (#152). iSolarCloud only updates every ~5 min, so a few minutes of
# staleness is harmless.
AVAILABILITY_GRACE_SECONDS = 900

_LOGGER = logging.getLogger(__name__)


# Developer-Portal whitelist rejections (Appendix 2). These must keep RETRYING rather
# than trigger reauth — re-authorizing can't add an IP/user to the app's whitelist — even
# though pysolarcloud >=0.9.0 types E919 as an ``AuthError``. Guarded ahead of the
# ``isinstance`` check in ``is_auth_error`` so that typing never overrides this.
WHITELIST_ERRORS = frozenset({"E918", "E919"})


def is_auth_error(err: Exception) -> bool:
    """Return True if the error means the stored credentials are no longer valid.

    pysolarcloud >=0.9.0 raises a typed ``AuthError`` for the documented dead-credential
    result codes (E00003/E900/E912/E914 — E919 too, but that is a whitelist code handled
    below), so those are matched via ``isinstance`` and need no per-code list here. The
    non-typed failures — a failed token refresh (``TokenRefreshError``), the OAuth
    ``invalid_grant``/``invalid_token`` errors, and ``auth_not_initialised`` — are matched
    by string via ``AUTH_ERRORS``. All require the user to re-authorize.

    Whitelist rejections (E918/E919) are explicitly excluded: they are Developer-Portal
    config issues that reauth cannot fix, so they stay transient (retry) despite E919's
    ``AuthError`` typing.
    """
    if not isinstance(err, PySolarCloudException):
        return False
    if err.error in WHITELIST_ERRORS:
        return False
    return isinstance(err, AuthError) or err.error in AUTH_ERRORS


# Actionable hints for otherwise-opaque iSolarCloud result codes (Appendix 2). These all
# keep retrying rather than reauth, so without a hint the raw code would just repeat in the
# log with no explanation of the cause or fix. Covers the whitelist rejections (E918/E919)
# and the API quota limits (E998/E999, pysolarcloud's ``RateLimitError``).
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
    "E998": (
        "iSolarCloud rejected the request: the monthly API call limit has been reached (E998). "
        "The integration will keep retrying; it recovers when the quota resets."
    ),
    "E999": (
        "iSolarCloud rejected the request: the hourly API call limit has been reached (E999). "
        "The integration will keep retrying; increase the polling interval in the integration "
        "options to make fewer calls."
    ),
}


def describe_api_error(err: Exception) -> str | None:
    """Return an actionable message for a known iSolarCloud error code, else None."""
    if isinstance(err, PySolarCloudException) and err.error is not None:
        return API_ERROR_HINTS.get(err.error)
    return None


# iSolarCloud error codes that warrant a user-facing Repair (#153). Each maps to a
# translation key under ``issues.<key>``. Reauth is intentionally absent: HA already opens
# a reauth flow when the coordinator raises ConfigEntryAuthFailed.
_REPAIR_CODES: dict[str, frozenset[str]] = {
    "whitelist_rejection": WHITELIST_ERRORS,
    "rate_limited": frozenset({"E998", "E999"}),
}
_REPAIR_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"


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
        # Monotonic timestamp of the last successful realtime poll; drives the
        # availability grace window (#152). None until the first success.
        self._last_successful_update: float | None = None
        self.devices: list[dict[str, Any]] = list(devices or [])
        self.extra_measure_points: dict[str, str] = dict(config_entry.options.get(CONF_EXTRA_MEASURE_POINTS, {}))
        self.enable_device_sensors: bool = bool(config_entry.options.get(CONF_ENABLE_DEVICE_SENSORS, False))
        # uuid -> { code: point } for per-device realtime (populated when enabled).
        self.device_data: dict[str, dict[str, Any]] = {}
        # Whether the dispatch device accepts parameter writes. Checked once at
        # setup; defaults True (fail-open) so an unavailable/unknown check never
        # hides working controls.
        self.dispatch_update_supported: bool = True
        # Whether the plant has a battery. Battery-only dispatch controls
        # (charge/discharge, SOC limits, forced-charge, battery-first) are hidden
        # when False: on a PV-only inverter they can't act and instead put it into
        # External-EMS mode, silently curtailing generation to ~0 (#148). Checked
        # once at setup; defaults True (fail-open) so a failed check never hides a
        # real battery user's controls.
        self.has_battery: bool = True

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
            # Surface actionable errors (whitelist, rate limit) as HA Repairs (#153).
            self._async_manage_repairs(err)
            # Transient failure (a timeout arrives here as TimeoutError). Rather than flap
            # every entity to "unavailable" on a brief cloud hiccup, keep serving the
            # last-good data while a recent success is still within the grace window (#152);
            # only give up once the data would be genuinely stale.
            if self.data is not None and self._within_availability_grace():
                _LOGGER.debug("Transient poll failure for %s; keeping last-good data: %s", self.plant_name, err)
                return self.data
            # A timeout arrives here as TimeoutError and is treated as transient.
            raise UpdateFailed(describe_api_error(err) or f"Error communicating with iSolarCloud API: {err}") from err

        self._async_manage_repairs(None)
        self._last_successful_update = self.hass.loop.time()

        # Refresh the device list so devices added to the plant after setup are
        # picked up at runtime (dynamic-devices) and removed ones can be pruned
        # (stale-devices). Throttled and best-effort: keep the previous list on failure.
        await self._async_maybe_refresh_devices()

        if self.enable_device_sensors:
            self.device_data = await self._async_fetch_device_data()

        # pysolarcloud is untyped, so the realtime payload is Any.
        return cast("dict[str, Any]", all_plants_data.get(self.plant_id, {}))

    def _within_availability_grace(self) -> bool:
        """True while the last successful poll is recent enough to keep serving stale data."""
        if self._last_successful_update is None:
            return False
        return (self.hass.loop.time() - self._last_successful_update) < AVAILABILITY_GRACE_SECONDS

    def _async_manage_repairs(self, err: Exception | None) -> None:
        """Raise or clear Repair issues for actionable iSolarCloud errors (#153).

        Called on every poll: creates the matching Repair when a known actionable code
        occurs and clears the rest, so a recovered plant automatically dismisses its issue.
        """
        code = err.error if isinstance(err, PySolarCloudException) else None
        for key, codes in _REPAIR_CODES.items():
            issue_id = f"{key}_{self.plant_id}"
            if code in codes:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=key,
                    translation_placeholders={"plant": self.plant_name},
                    learn_more_url=_REPAIR_LEARN_MORE,
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

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
            # Forward the ps_key of every device of this type. getDeviceRealTimeData is
            # keyed per-device and rejects the call with result_code 009 when neither
            # ps_key_list nor sn_list is supplied (pysolarcloud >=0.9.1). Passing None
            # lets the library discover the keys itself (an extra list call) for older
            # payloads that omit ps_key.
            ps_keys = [
                str(d["ps_key"])
                for d in self.devices
                if getattr(d.get("device_type"), "value", d.get("device_type")) == type_id and d.get("ps_key")
            ]
            # Inverter/ESS devices also report the diagnostic points (operating status,
            # MPPT, DC power, ...); request them on top of any user-configured extras (#149).
            # Battery/ESS devices likewise report battery points (SOC, temp, SOH, ...) (#154).
            extra = dict(self.extra_measure_points)
            if type_id in (DeviceType.INVERTER.value, DeviceType.ENERGY_STORAGE_SYSTEM.value):
                extra.update(INVERTER_DIAGNOSTIC_POINTS)
            if type_id in (DeviceType.BATTERY.value, DeviceType.ENERGY_STORAGE_SYSTEM.value):
                extra.update(BATTERY_DEVICE_POINTS)
            try:
                async with asyncio.timeout(self._poll_timeout):
                    result = await self.plants_service.async_get_device_realtime(
                        self.plant_id,
                        device_type,
                        ps_key_list=ps_keys or None,
                        extra_measure_points=extra or None,
                    )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("Per-device realtime failed for plant %s type %s: %s", self.plant_id, type_id, err)
                continue
            for uuid, points in (result or {}).items():
                merged.setdefault(str(uuid), {}).update(points)
        return merged
