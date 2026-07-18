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
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pysolarcloud import AuthError, PySolarCloudException, UserAuth
from pysolarcloud.plants import DeviceType, Plants

from .auth import AUTH_ERRORS
from .const import (
    BATTERY_DEVICE_POINTS,
    COMM_MODULE_POINTS,
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODBUS_UNIT,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_PORT,
    DEFAULT_MODBUS_UNIT,
    DEFAULT_SCAN_INTERVAL,
    DEVICE_REFRESH_INTERVAL,
    DOMAIN,
    ESS_BATTERY_POWER_POINTS,
    ESS_MPPT_DIAGNOSTIC_POINTS,
    ESS_OPERATING_STATUS_POINT,
    INVERTER_DIAGNOSTIC_POINTS,
    INVERTER_OPERATING_STATUS_POINT,
    METER_DEVICE_POINTS,
    STRING_MPPT_POINTS,
    TRANSPORT_MODBUS_ONLY,
)
from .energy_units import normalize_energy_units, normalize_power_units, tag_source
from .model_capabilities import mppt_points_for_model, resolve_capabilities

# Upper bound on a single poll's cloud calls, so a hung request can neither stall
# the coordinator indefinitely nor let successive polls pile up.
MAX_POLL_TIMEOUT = 60

# How long (seconds) to keep serving the last-good data — staying "available" — when
# polls fail transiently, before marking entities unavailable. Rides out the
# intermittent cloud/device hiccups that would otherwise flap every entity several
# times a minute (#152). iSolarCloud only updates every ~5 min, so a few minutes of
# staleness is harmless.
AVAILABILITY_GRACE_SECONDS = 900

# Ceiling for the backed-off poll interval: each rate-limited poll doubles the interval up
# to this cap, and a successful poll restores the configured interval (#156).
BACKOFF_MAX_INTERVAL = timedelta(hours=1)

_LOGGER = logging.getLogger(__name__)


# Developer-Portal whitelist rejections (Appendix 2). These must keep RETRYING rather
# than trigger reauth — re-authorizing can't add an IP/user to the app's whitelist — even
# though pysolarcloud >=0.9.0 types E919 as an ``AuthError``. Guarded ahead of the
# ``isinstance`` check in ``is_auth_error`` so that typing never overrides this.
WHITELIST_ERRORS = frozenset({"E918", "E919"})

# iSolarCloud quota/throttle codes (E998 monthly, E999 hourly). On these the coordinator
# backs off its poll interval rather than hammering the API (#156).
RATE_LIMIT_ERRORS = frozenset({"E998", "E999"})


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


def is_rate_limit_error(err: Exception) -> bool:
    """Return True if the error is an iSolarCloud quota/throttle rejection (E998/E999)."""
    return isinstance(err, PySolarCloudException) and err.error in RATE_LIMIT_ERRORS


# iSolarCloud error codes that warrant a user-facing Repair (#153). Each maps to a
# translation key under ``issues.<key>``. Reauth is intentionally absent: HA already opens
# a reauth flow when the coordinator raises ConfigEntryAuthFailed.
_REPAIR_CODES: dict[str, frozenset[str]] = {
    "whitelist_rejection": WHITELIST_ERRORS,
    "rate_limited": RATE_LIMIT_ERRORS,
}
_REPAIR_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"


class SungrowPlantCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching data from a single plant."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        plants_service: Plants | None,
        plant_id: str,
        plant_name: str,
        devices: list[dict[str, Any]] | None = None,
        user_auth: UserAuth | None = None,
    ) -> None:
        """Initialize the coordinator.

        ``plants_service`` is ``None`` for a cloud-free entry: either a Modbus-only entry
        (data comes from the local Modbus client, #159) or a cloud user-account entry
        (``user_auth`` set, data comes from the app/web API, #268).
        """
        scan_seconds = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"Sungrow Plant {plant_name}",
            update_interval=timedelta(seconds=scan_seconds),
            config_entry=config_entry,
        )
        self.plants_service = plants_service
        # UserAuth-backed client for a cloud user-account entry (#268); None otherwise.
        self._user_auth = user_auth
        self.plant_id = plant_id
        self.plant_name = plant_name
        # The user-configured poll interval, restored after a rate-limit back-off (#156).
        self._base_update_interval = timedelta(seconds=scan_seconds)
        # Cap each poll's requests at the scan interval, never longer than 60 s.
        self._poll_timeout: float = min(scan_seconds, MAX_POLL_TIMEOUT)
        # Monotonic timestamp of the last device-list refresh; None until first poll.
        self._last_device_refresh: float | None = None
        # Monotonic timestamp of the last plant-detail refresh; None until first poll.
        self._last_plant_detail_refresh: float | None = None
        # Plant-detail fields (alarm/fault counts, nameplate power, tariffs, ...) from
        # getPowerStationDetail, surfaced as plant-level sensors (#178). Empty until the
        # first successful fetch.
        self.plant_detail: dict[str, Any] = {}
        # Monotonic timestamp of the last successful realtime poll; drives the
        # availability grace window (#152). None until the first success.
        self._last_successful_update: float | None = None
        self.devices: list[dict[str, Any]] = list(devices or [])
        self.extra_measure_points: dict[str, str] = dict(config_entry.options.get(CONF_EXTRA_MEASURE_POINTS, {}))
        self.enable_device_sensors: bool = bool(config_entry.options.get(CONF_ENABLE_DEVICE_SENSORS, False))
        # uuid -> { code: point } for per-device realtime (populated when enabled).
        self.device_data: dict[str, dict[str, Any]] = {}
        # Device types that returned "unsupported" on a previous poll. Skipped on
        # subsequent polls to avoid wasting API quota on endpoints that don't exist
        # for this account/region (#288).
        self._unsupported_device_types: set[Any] = set()
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
        # How long (minutes) a forced Charge/Discharge command stays active before the
        # command select auto-reverts it to Stop, so a forced command can't silently
        # persist and curtail PV (#157/#148). 0 disables auto-revert (legacy behaviour).
        # Owned by the "Forced Dispatch Duration" number; read by the command select.
        self.forced_dispatch_duration_minutes: float = 0
        # Local Modbus client is only built for Modbus-only entries (cloud-free). Cloud
        # entries never attach Modbus — hybrid merge was removed in favour of a separate
        # local config entry with a soft device link (serial / via_device).
        self._modbus_client = self._build_modbus_client(config_entry)
        # Optional plant-device parent for device-registry nesting when a cloud plant
        # already owns this inverter serial (set by Modbus-only setup).
        self.via_plant_id: str | None = None
        # WiNet-S web UI URL for local inverter DeviceInfo (Modbus-only).
        self.local_configuration_url: str | None = None
        # Raw-wire diagnostic for #223 (daily_yield register window). Populated on each
        # successful Modbus poll and surfaced on the daily_yield sensor for inspection.
        # The *entity value* is no longer taken from that register — see
        # ``_async_apply_derived_daily_yield`` (SG-RS firmware never resets wire 5002).
        self.daily_yield_diagnostic: dict[str, Any] | None = None
        # Local Modbus diagnostics surfaced in the config-entry diagnostics download:
        # detected family, unsupported register blocks skipped, and the last error string.
        self.modbus_diagnostics: dict[str, Any] = {}
        # Persisted baseline for deriving daily_yield from total_yield when Modbus is used.
        self._daily_yield_store: Store[dict[str, Any]] | None = (
            Store(hass, 1, f"{DOMAIN}.daily_yield_baseline_{self.plant_id}")
            if self._modbus_client is not None
            else None
        )
        self._daily_yield_baseline_loaded = False
        # Imported lazily-typed to avoid a circular import at module load; set on first use.
        self._daily_yield_state: Any = None

    @staticmethod
    def _build_modbus_client(config_entry: ConfigEntry) -> Any:
        """Return a SungrowModbusClient for a Modbus-only entry, else None.

        Cloud entries never get a Modbus client (no hybrid overlay). The WiNet-S host
        lives in entry data for discovery/import-created local entries (#159).
        """
        if config_entry.data.get(CONF_TRANSPORT) != TRANSPORT_MODBUS_ONLY:
            return None
        host = config_entry.options.get(CONF_MODBUS_HOST) or config_entry.data.get(CONF_MODBUS_HOST)
        if not host:
            return None
        from .modbus import SungrowModbusClient
        from .model_capabilities import ModelFamily, resolve_model_family

        # Prefer a register-map family derived from the configured model code (e.g.
        # SH10RT-20 → sh_rt) so hybrids don't start on the SG-RS map before reg 5000
        # auto-detect runs (#219). Unknown models keep the sg_rs default.
        model_code = config_entry.data.get(CONF_MODEL) or config_entry.options.get(CONF_MODEL)
        family = resolve_model_family(str(model_code) if model_code else None)
        model = family.value if family is not ModelFamily.UNKNOWN else "sg_rs"

        return SungrowModbusClient(
            str(host),
            port=int(config_entry.options.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT)),
            unit=int(config_entry.options.get(CONF_MODBUS_UNIT, DEFAULT_MODBUS_UNIT)),
            model=model,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the API for this plant."""
        # Cloud-free entries have no Plants service: user-account (app/web) or Modbus.
        if self.plants_service is None:
            if self._user_auth is not None:
                return await self._async_user_update()
            return await self._async_modbus_only_update()
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
            # Surface actionable errors (whitelist, rate limit) as HA Repairs (#153) and
            # back off the poll interval while rate-limited (#156). Deliberately raise-only:
            # a transient error (e.g. a network TimeoutError) that interleaves with an
            # active rate-limit must NOT dismiss the still-valid Repair or reset the
            # back-off and resume hammering the API — only a *successful* poll clears the
            # Repairs and restores the interval.
            self._async_raise_repair(err)
            if is_rate_limit_error(err):
                self._adjust_poll_backoff(rate_limited=True)
            # Transient failure (a timeout arrives here as TimeoutError). Rather than flap
            # every entity to "unavailable" on a brief cloud hiccup, keep serving the
            # last-good data while a recent success is still within the grace window (#152);
            # only give up once the data would be genuinely stale.
            if self.data is not None and self._within_availability_grace():
                _LOGGER.debug("Transient poll failure for %s; keeping last-good data: %s", self.plant_name, err)
                return self.data
            # A timeout arrives here as TimeoutError and is treated as transient.
            raise UpdateFailed(describe_api_error(err) or f"Error communicating with iSolarCloud API: {err}") from err

        # Success: the plant recovered, so clear any Repairs and restore the interval.
        self._async_clear_repairs()
        self._adjust_poll_backoff(rate_limited=False)
        self._last_successful_update = self.hass.loop.time()

        # Refresh the device list so devices added to the plant after setup are
        # picked up at runtime (dynamic-devices) and removed ones can be pruned
        # (stale-devices). Throttled and best-effort: keep the previous list on failure.
        await self._async_maybe_refresh_devices()

        # Refresh the plant-detail fields (alarm/fault counts, nameplate, tariffs) for
        # the plant-level diagnostic sensors (#178). Throttled and best-effort.
        await self._async_maybe_refresh_plant_detail()

        # Always fetch per-device data: even with per-device sensors off, we request
        # each inverter/ESS device's operating status so the Fault binary sensor can
        # show a human-readable reason (#182). The heavy diagnostic sets are still gated
        # on the option inside the fetch.
        raw_devices = await self._async_fetch_device_data()
        self.device_data = {
            uuid: normalize_energy_units(tag_source(points, "cloud")) for uuid, points in raw_devices.items()
        }

        # pysolarcloud is untyped, so the realtime payload is Any.
        cloud_data = cast("dict[str, Any]", all_plants_data.get(self.plant_id, {}))
        return normalize_energy_units(tag_source(cloud_data, "cloud"))

    async def _async_modbus_only_update(self) -> dict[str, Any]:
        """Read realtime data from the local Modbus client only (cloud-free entry, #159)."""
        if self._modbus_client is None:
            raise UpdateFailed("Modbus-only entry has no Modbus client configured")
        try:
            async with asyncio.timeout(self._poll_timeout):
                data = await self._modbus_client.async_read_realtime()
        except Exception as err:  # pylint: disable=broad-except
            # Ride out a brief local blip the same way the cloud path does (#152).
            if self.data is not None and self._within_availability_grace():
                _LOGGER.debug("Transient Modbus read failure for %s; keeping last-good data: %s", self.plant_name, err)
                return self.data
            raise UpdateFailed(f"Local Modbus read failed: {err}") from err
        self._last_successful_update = self.hass.loop.time()
        self.modbus_diagnostics = dict(self._modbus_client.modbus_diagnostics)
        await self._async_capture_daily_yield_diagnostic()
        data = normalize_energy_units(cast("dict[str, Any]", data))
        return await self._async_apply_derived_daily_yield(data)

    async def _async_user_update(self) -> dict[str, Any]:
        """Poll a cloud user-account entry via the app/web API (#268/#269).

        Fetches the plant detail (``getPsDetail``) and maps it onto the measure-point
        model. A dead credential (``AuthError``) triggers reauth; a transient failure
        rides out the availability grace window like the other paths.
        """
        from .user_realtime import map_plant_detail_to_points

        assert self._user_auth is not None
        try:
            async with asyncio.timeout(self._poll_timeout):
                detail = await self._user_auth.async_get_plant_detail(self.plant_id)
        except Exception as err:  # pylint: disable=broad-except
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(f"iSolarCloud user-account login failed: {err}") from err
            if self.data is not None and self._within_availability_grace():
                _LOGGER.debug("Transient user-account poll failure for %s; keeping last-good: %s", self.plant_name, err)
                return self.data
            raise UpdateFailed(f"iSolarCloud user-account poll failed: {err}") from err
        self._last_successful_update = self.hass.loop.time()
        points = map_plant_detail_to_points(detail)
        return normalize_power_units(normalize_energy_units(tag_source(points, "cloud_user")))

    async def _async_apply_derived_daily_yield(self, data: dict[str, Any]) -> dict[str, Any]:
        """Replace Modbus ``daily_yield`` with total_yield − start-of-local-day baseline.

        SG-RS wire 5002 does not reset at midnight on observed firmware; lifetime
        ``total_yield`` is trustworthy. Baseline is persisted so a restart mid-day
        keeps counting from the same day start.
        """
        from .daily_yield import DailyYieldBaseline, apply_derived_daily_yield

        if self._daily_yield_store is None:
            return data
        if not self._daily_yield_baseline_loaded:
            stored = await self._daily_yield_store.async_load()
            self._daily_yield_state = DailyYieldBaseline.from_store(stored)
            self._daily_yield_baseline_loaded = True
        if self._daily_yield_state is None:
            self._daily_yield_state = DailyYieldBaseline()

        local_date = dt_util.now().date()
        data, new_state, daily = apply_derived_daily_yield(data, local_date=local_date, state=self._daily_yield_state)
        if daily is None:
            return data
        if new_state.to_store() != self._daily_yield_state.to_store():
            self._daily_yield_state = new_state
            await self._daily_yield_store.async_save(new_state.to_store())
        else:
            self._daily_yield_state = new_state
        return data

    async def _async_capture_daily_yield_diagnostic(self) -> None:
        """Best-effort capture of the raw daily_yield register window (opt-in).

        Off by default: the dump is ~2 KB per state write and would bloat the recorder.
        Enable via options → ``modbus_debug_daily_yield`` when investigating register maps.
        """
        if self._modbus_client is None:
            return
        entry = self.config_entry
        if entry is None or not entry.options.get(CONF_MODBUS_DEBUG_DAILY_YIELD, False):
            self.daily_yield_diagnostic = None
            return
        try:
            async with asyncio.timeout(self._poll_timeout):
                self.daily_yield_diagnostic = await self._modbus_client.async_read_daily_yield_diagnostic()
        except Exception as err:  # pylint: disable=broad-except  (best-effort diagnostic)
            _LOGGER.debug("daily_yield diagnostic capture failed for %s: %s", self.plant_name, err)

    def _within_availability_grace(self) -> bool:
        """True while the last successful poll is recent enough to keep serving stale data."""
        if self._last_successful_update is None:
            return False
        return (self.hass.loop.time() - self._last_successful_update) < AVAILABILITY_GRACE_SECONDS

    def _adjust_poll_backoff(self, *, rate_limited: bool) -> None:
        """Back off the poll interval on rate-limit errors, restoring it on recovery (#156).

        Each rate-limited poll doubles the interval up to ``BACKOFF_MAX_INTERVAL``, so the
        integration stops hammering iSolarCloud once it hits the hourly/monthly quota; the
        next successful poll restores the user's configured interval.
        """
        if rate_limited:
            current = self.update_interval or self._base_update_interval
            new = min(current * 2, BACKOFF_MAX_INTERVAL)
            if new != self.update_interval:
                self.update_interval = new
                _LOGGER.warning("iSolarCloud rate-limited %s; backing off poll interval to %s", self.plant_name, new)
        elif self.update_interval != self._base_update_interval:
            self.update_interval = self._base_update_interval
            _LOGGER.info(
                "iSolarCloud recovered for %s; poll interval restored to %s",
                self.plant_name,
                self._base_update_interval,
            )

    def _async_raise_repair(self, err: Exception) -> None:
        """Raise the Repair matching this error's actionable code, if any (#153).

        Only *creates* the matching issue; it never clears others, so a transient error
        (a plain ``TimeoutError`` has no code) can't dismiss a still-valid rate-limit or
        whitelist Repair. Repairs are cleared only on a successful poll, via
        :meth:`_async_clear_repairs`.
        """
        code = err.error if isinstance(err, PySolarCloudException) else None
        if code is None:
            return
        for key, codes in _REPAIR_CODES.items():
            if code in codes:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"{key}_{self.plant_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=key,
                    translation_placeholders={"plant": self.plant_name},
                    learn_more_url=_REPAIR_LEARN_MORE,
                )

    def _async_clear_repairs(self) -> None:
        """Clear all managed Repair issues — called on a successful poll (#153)."""
        for key in _REPAIR_CODES:
            ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.plant_id}")

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
        assert self.plants_service is not None  # only reached on the cloud path
        try:
            async with asyncio.timeout(self._poll_timeout):
                devices = await self.plants_service.async_get_plant_devices(self.plant_id)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not refresh devices for plant %s: %s", self.plant_id, err)
            return
        # Mutate in place so holders of this list (runtime_data.devices) see updates.
        self.devices[:] = list(devices or [])

    async def _async_maybe_refresh_plant_detail(self) -> None:
        """Refresh plant-detail fields periodically rather than on every poll (#178).

        The plant-detail payload (nameplate, tariffs, alarm/fault counts) changes slowly,
        so it's re-fetched on the same cadence as the device list to save quota.
        """
        now = self.hass.loop.time()
        if (
            self._last_plant_detail_refresh is not None
            and (now - self._last_plant_detail_refresh) < DEVICE_REFRESH_INTERVAL
        ):
            return
        self._last_plant_detail_refresh = now
        await self._async_refresh_plant_detail()

    async def _async_refresh_plant_detail(self) -> None:
        """Re-fetch the plant-detail fields (best effort, non-fatal)."""
        assert self.plants_service is not None  # only reached on the cloud path
        try:
            async with asyncio.timeout(self._poll_timeout):
                details = await self.plants_service.async_get_plant_details(self.plant_id)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not refresh plant detail for plant %s: %s", self.plant_id, err)
            return
        for row in details or []:
            self.plant_detail = dict(row)
            return

    async def _async_fetch_device_data(self) -> dict[str, dict[str, Any]]:
        """Fetch per-device realtime for each distinct device type (best effort).

        The plant realtime endpoint only returns the plant-level points, so devices
        like EV chargers or meters need a per-device fetch (issue #74). This is
        best-effort: a device type whose endpoint is unavailable or errors simply
        contributes nothing rather than failing the whole update. Any user-configured
        extra measure points are requested here too, so newly identified charger/meter
        point IDs surface without a code change.
        """
        assert self.plants_service is not None  # only reached on the cloud path
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
            # Skip device types that were previously marked unsupported (#288).
            if type_id in self._unsupported_device_types:
                continue
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
            # Resolve the inverter family from the model code (#251). The cloud sometimes
            # types a hybrid as a plain INVERTER; the model's battery signal is used to
            # request battery/MPPT points that the device-type heuristic alone would miss.
            model_code = device.get("device_model_code")
            caps = resolve_capabilities(model_code)
            is_ess = type_id == DeviceType.ENERGY_STORAGE_SYSTEM.value

            extra: dict[str, str] = {}
            # Always request the operating-status point for inverters/ESS so the Fault
            # binary sensor can surface a reason regardless of the device-sensor option
            # (#182). Inverters use point 29, ESS/hybrids 13146.
            if is_ess:
                extra.update(ESS_OPERATING_STATUS_POINT)
                # Always request battery charge/discharge power for ESS devices so hybrid
                # users see separate charge and discharge power sensors (#31).
                extra.update(ESS_BATTERY_POWER_POINTS)
            elif type_id == DeviceType.INVERTER.value:
                extra.update(INVERTER_OPERATING_STATUS_POINT)
                # A hybrid the cloud typed as a plain inverter still has a battery — request
                # its charge/discharge power so those sensors appear without manual config
                # (#31/#251). The battery power ids are ESS-specific, so a true string
                # inverter (has_battery False) never requests them.
                if caps.has_battery is True:
                    extra.update(ESS_BATTERY_POWER_POINTS)
            # The full diagnostic/battery/meter/comm sets (and user extras) are only
            # fetched when the user has opted into per-device sensors (#149/#154/#179).
            # With the option on, an unmapped device type still gets a best-effort fetch
            # (extra=None -> the default measure points) as before.
            if self.enable_device_sensors:
                extra.update(self.extra_measure_points)
                if type_id in (DeviceType.INVERTER.value, DeviceType.ENERGY_STORAGE_SYSTEM.value):
                    diagnostic = dict(INVERTER_DIAGNOSTIC_POINTS)
                    # Pick the MPPT id range by model family when known (#251): SG-family
                    # string inverters report MPPT on points 5-10, SH-family hybrids on a
                    # separate 13xxx range. Both ranges share the mpptN_* codes, so mixing
                    # them would map two ids to one code and silently overwrite each other
                    # in the per-device merge — hence we swap the range wholesale rather
                    # than union it. Falls back to the device-type heuristic for unknown
                    # models so nothing regresses.
                    model_mppt = mppt_points_for_model(model_code)
                    if model_mppt:
                        for pid in set(STRING_MPPT_POINTS) | set(ESS_MPPT_DIAGNOSTIC_POINTS):
                            diagnostic.pop(pid, None)
                        diagnostic.update(model_mppt)
                    elif is_ess:
                        for pid in STRING_MPPT_POINTS:
                            diagnostic.pop(pid, None)
                        diagnostic.update(ESS_MPPT_DIAGNOSTIC_POINTS)
                    if is_ess:
                        # An ESS reports operating status on 13146 (already requested above);
                        # drop the inverter point 29 so the two don't collide on the shared
                        # "operating_status" code and silently overwrite each other (#182).
                        diagnostic.pop("29", None)
                    extra.update(diagnostic)
                # Battery device points for an ESS/battery device, or a hybrid the cloud
                # typed as a plain inverter (model says it has a battery) (#251).
                if type_id in (DeviceType.BATTERY.value, DeviceType.ENERGY_STORAGE_SYSTEM.value) or (
                    type_id == DeviceType.INVERTER.value and caps.has_battery is True
                ):
                    extra.update(BATTERY_DEVICE_POINTS)
                # Communication modules report WLAN/wireless signal strength (#149).
                if type_id == DeviceType.COMMUNICATION_MODULE.value:
                    extra.update(COMM_MODULE_POINTS)
                # Energy meters report instantaneous power / PF / frequency / per-phase (#179).
                if type_id == DeviceType.METER.value:
                    extra.update(METER_DEVICE_POINTS)
            elif not extra:
                # Device sensors off and this type reports no operating status
                # (battery/meter/comm/unknown) — nothing to fetch, skip it.
                continue
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
            if not result:
                # The endpoint is unavailable for this device type; skip on future polls.
                self._unsupported_device_types.add(type_id)
                continue
            for uuid, points in result.items():
                merged.setdefault(str(uuid), {}).update(points)
        return merged
