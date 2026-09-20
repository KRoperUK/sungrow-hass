"""Data update coordinator for the Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, cast

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pysolarcloud import AuthError, DeviceEndpointUnavailable, PySolarCloudException, RateLimitError, UserAuth
from pysolarcloud.plants import DeviceType, Plants

from .api_rate import (
    CALL_TYPE_DEVICE_LIST,
    CALL_TYPE_DEVICE_REALTIME,
    CALL_TYPE_PLANT_DETAIL,
    CALL_TYPE_REALTIME,
    CALL_TYPE_USER_DEVICE_FETCH,
    ApiCallRateTracker,
    label_for,
)
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
from .modbus import SungrowModbusError
from .modbus_registers import needs_derived_daily_yield
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


# Plausible per-device battery charge/discharge POWER field names in the app battery
# endpoints (``getBatteryCapacityByPsIdV2`` / ``getPsBatteryInfo``).
#
# UNVERIFIED against a live device (#450): the decompiled app's
# ``getBatteryCapacityByPsIdV2`` response (``BatteryCapacityVOOversea``) carries the
# battery *type* and *capacity* (kWh) — a settings screen — **not** a charge/discharge
# power (W) limit, and ``getPsBatteryInfo``'s field set is documented unverified in the
# library. So :func:`resolve_battery_power_limit_w` is best-effort feature-detection: it
# returns ``None`` for the observed capacity shape (leaving the datasheet/model-code
# nameplate resolution in ``number.py`` untouched), and only overrides the dispatch
# ceiling when a real, unit-qualified power field actually appears in a payload.
_BATTERY_POWER_FIELD_HINTS = (
    "max_charge_power",
    "max_discharge_power",
    "charge_power",
    "discharge_power",
    "rated_power",
)


def _coerce_power_watts(raw: Any) -> int | None:
    """Coerce a battery-endpoint power field to watts, or ``None`` when not confidently a power.

    Accepts either a scalar or an app-style ``{"value", "unit"}`` dict. Converts an
    explicit ``kW`` unit ×1000; trusts an explicit ``W`` unit as-is. With no/unknown unit
    it only accepts a value already in a plausible watt range (300–100 000 W) so a bare
    ``kW`` figure (e.g. ``10``) is rejected rather than mis-scaled 1000×. The unit-less
    branch is live-untested (#450).
    """
    value: Any = raw
    unit: Any = None
    if isinstance(raw, dict):
        value, unit = raw.get("value"), raw.get("unit")
    try:
        num = float(value)
    except TypeError, ValueError:
        return None
    if num <= 0:
        return None
    u = str(unit or "").strip().lower()
    if u == "kw":
        return int(round(num * 1000))
    if u == "w":
        return int(round(num))
    if 300 <= num <= 100_000:
        return int(round(num))
    return None


def resolve_battery_power_limit_w(*payloads: dict[str, Any] | None) -> int | None:
    """Return a real battery charge/discharge power ceiling (W) from battery payloads, or ``None``.

    Scans each payload's top-level keys for a recognised power field
    (:data:`_BATTERY_POWER_FIELD_HINTS`) and takes the largest confidently-watt value
    (charge and discharge sliders share one ceiling). Returns ``None`` when no such field
    is present — the expected outcome for the observed capacity-only shape — so callers
    fall back to the existing nameplate resolution. See the note on
    :data:`_BATTERY_POWER_FIELD_HINTS` for why this is best-effort.
    """
    best: int | None = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key, raw in payload.items():
            if not any(hint in str(key).lower() for hint in _BATTERY_POWER_FIELD_HINTS):
                continue
            watts = _coerce_power_watts(raw)
            if watts is not None and (best is None or watts > best):
                best = watts
    return best


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
    """Return True if the error is an iSolarCloud quota/throttle rejection.

    Matched by type first — pysolarcloud types the codes it knows as ``RateLimitError``, so
    a code added upstream starts backing off without an integration change. ``E998``/``E999``
    stay listed because the code itself is still needed to name the Repair and pick the hint.
    """
    if isinstance(err, RateLimitError):
        return True
    return isinstance(err, PySolarCloudException) and err.error in RATE_LIMIT_ERRORS


def rate_limit_retry_after(err: Exception) -> float | None:
    """Return the server-suggested back-off in seconds, when the error carries one (#458).

    iSolarCloud sometimes states how long to wait; the typed error surfaces that hint so the
    integration can back off precisely instead of guessing at a doubling interval. ``None``
    when the API said nothing, which leaves the doubling behaviour in charge.
    """
    if not isinstance(err, RateLimitError):
        return None
    value = err.retry_after
    if value is None or value <= 0:
        return None
    return float(value)


# iSolarCloud error codes that warrant a user-facing Repair (#153). Each maps to a
# translation key under ``issues.<key>``. Reauth is intentionally absent: HA already opens
# a reauth flow when the coordinator raises ConfigEntryAuthFailed.
_REPAIR_CODES: dict[str, frozenset[str]] = {
    "whitelist_rejection": WHITELIST_ERRORS,
    "rate_limited": RATE_LIMIT_ERRORS,
}
_REPAIR_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"

# Translation key for the proactive API-rate warning Repair (#434). Unlike the reactive
# ``rate_limited`` Repair (raised only after iSolarCloud returns E999), this one fires
# while the observed rate is merely *approaching* the budget, so the user can widen the
# poll interval before requests start being rejected.
_RATE_WARNING_ISSUE = "api_rate_warning"


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
        rate_tracker: ApiCallRateTracker | None = None,
    ) -> None:
        """Initialize the coordinator.

        ``plants_service`` is ``None`` for a cloud-free entry: either a Modbus-only entry
        (data comes from the local Modbus client, #159) or a cloud user-account entry
        (``user_auth`` set, data comes from the app/web API, #268).

        ``rate_tracker`` counts the outbound API calls this coordinator makes so the
        integration can warn before the iSolarCloud quota is exhausted (#434). One tracker
        is shared across a config entry's plant coordinators (the budget is per account),
        passed in by ``__init__.py``. When omitted, a cloud transport creates its own so
        direct construction (and tests) still tracks; a Modbus-only entry gets none —
        local reads spend no API budget.
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
        # True for the cloud user-account transport (app/web API). Lets the entity
        # builders tell cloud_user apart from a Modbus-only entry (both have no
        # ``plants_service``) so cloud_user is routed down the cloud entity path, not the
        # local-Modbus one (#456/#457).
        self.uses_user_api: bool = user_auth is not None
        # Rolling-window counter of the outbound API calls this coordinator makes,
        # tagged by call type, so the integration can warn as the observed rate
        # approaches the iSolarCloud budget (#434). Only cloud transports get one:
        # Modbus reads spend no API budget. A shared tracker is passed in by
        # ``__init__.py`` (the budget is per account, shared across the entry's plants);
        # when constructed directly (tests), a cloud transport makes its own.
        is_cloud = plants_service is not None or user_auth is not None
        self.rate_tracker: ApiCallRateTracker | None = rate_tracker or (
            ApiCallRateTracker(time_fn=hass.loop.time) if is_cloud else None
        )
        # Latched once the proactive rate Repair is raised, so it is cleared exactly once
        # when the observed rate falls back under the threshold.
        self._rate_repair_raised = False
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
        # One-shot latch for a failed per-device refresh (#439). Worth telling the user
        # about once, since it silently costs them every per-device sensor — but not on
        # every poll.
        self._device_refresh_warned = False
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
        # Real battery charge/discharge power ceiling (W) resolved from the app battery
        # endpoints, or None when unavailable/unconfirmed — see #450 and
        # ``async_probe_battery_power_limit``. When set it overrides the datasheet/model
        # nameplate ceiling for the battery power slider; when None the existing
        # resolution (number.py) is preserved unchanged.
        self.battery_power_limit_w: int | None = None
        # Raw battery-capacity payload (app ``getBatteryCapacityByPsIdV2``) for diagnostics;
        # empty until the cloud_user setup probe runs. Nameplate/usable capacity (kWh) and
        # battery type — not a power figure (#450).
        self.battery_capacity: dict[str, Any] = {}
        # Latest fault detail from the app fault API (cloud_user only, #457). Best-effort:
        # ``fault_list`` is the most recent page of open faults (``queryFaultList``) and
        # ``fault_summary`` the raw ``getDevFaultCountByPsId`` payload. Both stay empty when
        # the endpoints are unavailable. These only *enrich* the plant Fault binary sensor's
        # attributes; the sensor's on/off is driven by the reliable fault/alarm counts, so a
        # missing fault API never changes the problem state.
        self.fault_list: list[dict[str, Any]] = []
        self.fault_summary: dict[str, Any] = {}
        # EV charger ("charging pile") discovery + realtime for cloud_user (#456). Best-effort
        # and feature-detected: ``charging_piles`` is the discovered charger list (cached, it
        # is slow-changing metadata) and ``charger_data`` maps each charger uuid to its latest
        # realtime payload. Both stay empty when the account has no chargers or the endpoints
        # are absent, so nothing is created on plants without a charger.
        self.charging_piles: list[dict[str, Any]] = []
        self.charger_data: dict[str, dict[str, Any]] = {}
        self._charging_piles_loaded = False
        # How long (minutes) a forced Charge/Discharge command stays active before the
        # command select auto-reverts it to Stop, so a forced command can't silently
        # persist and curtail PV (#157/#148). 0 disables auto-revert (legacy behaviour).
        # Owned by the "Forced Dispatch Duration" number; read by the command select.
        self.forced_dispatch_duration_minutes: float = 0
        # Local Modbus client is only built for Modbus-only entries (cloud-free). Cloud
        # entries never attach Modbus — hybrid merge was removed in favour of a separate
        # local config entry with a soft device link (serial / via_device_id).
        self._modbus_client = self._build_modbus_client(config_entry)
        # Optional plant-device parent for device-registry nesting when a cloud plant
        # already owns this inverter serial (set by Modbus-only setup).
        self.via_device_id: str | None = None
        # WiNet-S web UI URL for local inverter DeviceInfo (Modbus-only).
        self.local_configuration_url: str | None = None
        # Raw-wire diagnostic for #223 (daily_yield register window). Populated on each
        # successful Modbus poll and surfaced on the daily_yield sensor for inspection.
        # The *entity value* is no longer taken from that register — see
        # ``_async_apply_derived_daily_values`` (SG-RS firmware never resets wire 5002).
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
        # Persisted baselines for deriving daily grid import/export from the lifetime
        # counters when the device's own daily register is absent or stuck at 0 (#471).
        # Separate from the yield baseline so one counter rolling over can't clobber the
        # other's stored payload.
        self._grid_daily_store: Store[dict[str, Any]] | None = (
            Store(hass, 1, f"{DOMAIN}.grid_daily_baseline_{self.plant_id}") if self._modbus_client is not None else None
        )
        self._grid_daily_baseline_loaded = False
        self._grid_daily_state: Any = None
        # Lifetime counters already warned about as reporting impossible values, so a
        # persistently broken meter logs once instead of every poll (#471).
        self._grid_glitch_warned: set[str] = set()

    async def async_remove_derived_daily_stores(self) -> None:
        """Delete this plant's persisted derivation baselines.

        Home Assistant does not remove a ``Store`` with the config entry it belongs to, so
        a deleted local entry would leave both baseline files orphaned in ``.storage``.

        Called only when the entry is *removed*, never on unload: unload happens on every
        reload (options change, HA restart), and dropping the baselines there would restart
        the day's derived figures from 0 (#471).
        """
        for store in (self._daily_yield_store, self._grid_daily_store):
            if store is not None:
                await store.async_remove()

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
            model_code=str(model_code) if model_code else None,
        )

    def close_modbus(self) -> None:
        """Release the WiNet-S Modbus TCP session (unload or failed setup).

        WiNet-S typically accepts only one concurrent client. Leaving a socket open
        after options reload / failed first_refresh causes every subsequent setup to
        fail with connection errors until HA or the dongle is restarted.
        """
        client = self._modbus_client
        if client is None:
            return
        self._modbus_client = None
        close = getattr(client, "close", None)
        if close is not None:
            close()

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
                self._record_api_call(CALL_TYPE_REALTIME)
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
                self._adjust_poll_backoff(rate_limited=True, retry_after=rate_limit_retry_after(err))
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
        # Proactively warn if this poll's calls push the observed rate toward the budget
        # (#434). Advisory only — never throttles or fails the poll.
        self._async_check_rate_budget()
        return normalize_energy_units(tag_source(cloud_data, "cloud"))

    async def _async_modbus_only_update(self) -> dict[str, Any]:
        """Read realtime data from the local Modbus client only (cloud-free entry, #159)."""
        if self._modbus_client is None:
            raise UpdateFailed("Modbus-only entry has no Modbus client configured")
        try:
            async with asyncio.timeout(self._poll_timeout):
                data = await self._modbus_client.async_read_realtime()
        except (SungrowModbusError, TimeoutError) as err:
            # Ride out a brief local blip the same way the cloud path does (#152).
            if self.data is not None and self._within_availability_grace():
                _LOGGER.debug("Transient Modbus read failure for %s; keeping last-good data: %s", self.plant_name, err)
                return self.data
            raise UpdateFailed(f"Local Modbus read failed: {err}") from err
        # Time since the previous successful poll, used to judge whether a lifetime
        # counter's latest step is physically possible (see the grid derivation).
        previous_update = self._last_successful_update
        self._last_successful_update = self.hass.loop.time()
        elapsed_seconds = None if previous_update is None else self._last_successful_update - previous_update
        self.modbus_diagnostics = dict(self._modbus_client.modbus_diagnostics)
        await self._async_capture_daily_yield_diagnostic()
        data = normalize_energy_units(cast("dict[str, Any]", data))
        return await self._async_apply_derived_daily_values(data, elapsed_seconds=elapsed_seconds)

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
                self._record_api_call(CALL_TYPE_REALTIME)
                detail = await self._user_auth.async_get_plant_detail(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(f"iSolarCloud user-account login failed: {err}") from err
            if self.data is not None and self._within_availability_grace():
                _LOGGER.debug("Transient user-account poll failure for %s; keeping last-good: %s", self.plant_name, err)
                return self.data
            raise UpdateFailed(f"iSolarCloud user-account poll failed: {err}") from err
        self._last_successful_update = self.hass.loop.time()
        await self._async_refresh_user_device_data()
        await self._async_refresh_faults()
        await self._async_refresh_chargers()
        points = map_plant_detail_to_points(detail)
        # Proactively warn if this poll's calls push the observed rate toward the budget
        # (#434). The per-poll device-list fetch is the dominant contributor here (#439).
        self._async_check_rate_budget()
        return normalize_power_units(normalize_energy_units(tag_source(points, "cloud_user")))

    async def _async_refresh_user_device_data(self) -> None:
        """Populate ``device_data`` from the user-API device list (best effort, #389).

        The app/web device-list response embeds each device's current ``point_data``, so
        it is the per-device realtime source on this transport and is re-fetched every
        poll — unlike the OAuth path, where the device list is slow-changing metadata
        refreshed on ``DEVICE_REFRESH_INTERVAL`` and realtime is a separate call.

        Gated on ``enable_device_sensors`` because nothing else on this transport
        consumes ``device_data``, so the extra call is only spent when it produces
        entities. A failure is non-fatal: the plant-level points still update and the last
        known per-device readings stay published, so a battery or meter does not vanish
        from the UI because one poll failed (#439).
        """
        if not self.enable_device_sensors:
            return
        assert self._user_auth is not None
        from .user_realtime import map_device_list_to_points

        try:
            async with asyncio.timeout(self._poll_timeout):
                self._record_api_call(CALL_TYPE_USER_DEVICE_FETCH)
                devices = await self._user_auth.async_get_devices(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            # Do not bail out. On this transport the device list *is* the per-device
            # realtime source, so returning here left ``device_data`` empty and silently
            # dropped every per-device entity — the only trace being a debug line nobody
            # reads (#439). Fall through and map the device list we already hold instead:
            # stale-but-present beats an entity that never appears, and it is the same
            # list the entity builders already trust for names and model codes.
            if self._device_refresh_warned:
                _LOGGER.debug("Could not refresh user-account devices for plant %s: %s", self.plant_id, err)
            else:
                self._device_refresh_warned = True
                _LOGGER.warning(
                    "Could not refresh the device list for plant %s (%s); per-device sensors keep their last "
                    "known values until the next successful poll",
                    self.plant_name,
                    err,
                )
            devices = None
        else:
            self._device_refresh_warned = False
        if devices:
            # Update the coordinator's own list in place: the entity builders read
            # ``coordinator.devices`` on every poll, so a battery that appears later
            # gets its sensors at runtime.
            self.devices[:] = list(devices)
        mapped = map_device_list_to_points(self.devices)
        self.device_data = {
            uuid: normalize_energy_units(tag_source(points, "cloud_user")) for uuid, points in mapped.items()
        }

    async def _async_refresh_faults(self) -> None:
        """Best-effort refresh of the plant fault detail from the app fault API (#457).

        cloud_user only. Populates ``fault_list`` (most recent open faults) and
        ``fault_summary`` (raw per-type counts) to enrich the plant Fault binary sensor's
        attributes. Every failure is non-fatal and leaves the last-known values in place —
        this never affects the sensor's on/off (that is driven by the reliable fault/alarm
        counts), so an unavailable fault API cannot flip the problem state.

        Gated on ``enable_device_sensors`` for parity with the other opt-in per-device
        fetches, so the extra calls are only spent when the user has asked for the richer
        entity set.
        """
        if self._user_auth is None or not self.enable_device_sensors:
            return
        try:
            async with asyncio.timeout(self._poll_timeout):
                self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                faults = await self._user_auth.async_query_faults(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            _LOGGER.debug("Fault list fetch failed for %s: %s", self.plant_name, err)
        else:
            self.fault_list = list(faults) if isinstance(faults, list) else []
        try:
            async with asyncio.timeout(self._poll_timeout):
                self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                summary = await self._user_auth.async_get_fault_count(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            _LOGGER.debug("Fault count fetch failed for %s: %s", self.plant_name, err)
        else:
            self.fault_summary = dict(summary) if isinstance(summary, dict) else {}

    async def _async_refresh_chargers(self) -> None:
        """Best-effort discovery + realtime for EV chargers on cloud_user (#456).

        The charger list (``getChargingPileList``) is slow-changing metadata, so it is
        fetched once and cached; each poll then refreshes every charger's realtime payload
        (``getChargingPileRealData``). Feature-detected and non-fatal: a plant with no
        chargers, or an account/region without the endpoints, leaves ``charging_piles`` and
        ``charger_data`` empty and no charger entities are created.

        Gated on ``enable_device_sensors`` — chargers are per-device entities, so the extra
        calls are only spent when the user opted into the richer entity set.
        """
        if self._user_auth is None or not self.enable_device_sensors:
            return
        if not self._charging_piles_loaded:
            try:
                async with asyncio.timeout(self._poll_timeout):
                    self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                    piles = await self._user_auth.async_get_charging_piles(self.plant_id)
            except (PySolarCloudException, ClientError, TimeoutError) as err:
                _LOGGER.debug("Charger discovery failed for %s: %s", self.plant_name, err)
                return
            self.charging_piles = [p for p in piles if isinstance(p, dict) and p.get("uuid") is not None]
            self._charging_piles_loaded = True
            if self.charging_piles:
                _LOGGER.debug("Discovered %d EV charger(s) for %s", len(self.charging_piles), self.plant_name)
        for pile in self.charging_piles:
            uuid = str(pile["uuid"])
            try:
                async with asyncio.timeout(self._poll_timeout):
                    self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                    data = await self._user_auth.async_get_charging_pile_realtime(pile["uuid"])
            except (PySolarCloudException, ClientError, TimeoutError, ValueError) as err:
                _LOGGER.debug("Charger realtime fetch failed for %s (%s): %s", uuid, self.plant_name, err)
                continue
            if isinstance(data, dict) and data:
                self.charger_data[uuid] = data

    async def async_probe_battery_power_limit(self) -> None:
        """Best-effort resolve the real battery charge/discharge power ceiling (#450).

        On the cloud_user transport the app battery endpoints
        (``getBatteryCapacityByPsIdV2`` / ``getPsBatteryInfo``) may expose the real
        per-device power limit. When a confident watt figure is found it is stored on
        ``battery_power_limit_w`` and overrides the datasheet/model-code nameplate ceiling
        for the charge/discharge slider (number.py); otherwise the attribute stays ``None``
        and the existing resolution is preserved. Called once at setup, guarded by
        ``has_battery``. Every failure is non-fatal — this only sizes a slider ceiling.

        .. note::
            The observed capacity payload carries kWh capacity + battery type, not a power
            field, so in practice this leaves the ceiling unchanged; it lights up only if a
            real power field ever appears. See ``resolve_battery_power_limit_w``.
        """
        if self._user_auth is None or not self.has_battery:
            return
        capacity: dict[str, Any] | None = None
        info: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self._poll_timeout):
                self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                capacity = await self._user_auth.async_get_battery_capacity(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            _LOGGER.debug("Battery capacity probe failed for %s: %s", self.plant_name, err)
        try:
            async with asyncio.timeout(self._poll_timeout):
                self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                info = await self._user_auth.async_get_battery_info(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            _LOGGER.debug("Battery info probe failed for %s: %s", self.plant_name, err)
        self.battery_capacity = capacity or {}
        limit = resolve_battery_power_limit_w(capacity, info)
        if limit is not None:
            self.battery_power_limit_w = limit
            _LOGGER.debug("Resolved real battery power ceiling for %s: %s W", self.plant_name, limit)

    async def _async_apply_derived_daily_values(
        self, data: dict[str, Any], *, elapsed_seconds: float | None = None
    ) -> dict[str, Any]:
        """Replace unreliable local-Modbus daily counters with derived values.

        Two derivations share the persisted-baseline mechanism, because both replace a
        daily register that the device cannot be trusted to reset at local midnight:

        * ``daily_yield`` from ``total_yield`` — only for families whose raw register is
          known-broken: the SH hybrids reset wire 13001 correctly, and overriding it would
          under-report until the first midnight after install (#382).
        * ``daily_imported_energy`` / ``daily_exported_energy`` from the lifetime grid
          counters when the device's own daily register is absent or stuck at 0 (#471).
          Deliberately not family-gated: the daily grid registers are firmware-dependent
          on SG and SH alike (#401), and a live non-zero register is still kept.

        Baselines are persisted so a restart mid-day keeps counting from the same day start.
        """
        from .derived_daily import (
            DerivedDailyBaseline,
            DerivedDailyEnergyState,
            apply_derived_daily_grid_energy,
            apply_derived_daily_yield,
        )

        if self._daily_yield_store is None:
            return data

        local_date = dt_util.now().date()
        family = getattr(self._modbus_client, "model", None)
        if needs_derived_daily_yield(family):
            if not self._daily_yield_baseline_loaded:
                self._daily_yield_state = DerivedDailyBaseline.from_store(await self._daily_yield_store.async_load())
                self._daily_yield_baseline_loaded = True
            if self._daily_yield_state is None:
                self._daily_yield_state = DerivedDailyBaseline()

            data, new_state, daily = apply_derived_daily_yield(
                data, local_date=local_date, state=self._daily_yield_state
            )
            if daily is not None and new_state.to_store() != self._daily_yield_state.to_store():
                await self._daily_yield_store.async_save(new_state.to_store())
            self._daily_yield_state = new_state

        if self._grid_daily_store is None:
            return data
        if not self._grid_daily_baseline_loaded:
            self._grid_daily_state = DerivedDailyEnergyState.from_store(await self._grid_daily_store.async_load())
            self._grid_daily_baseline_loaded = True
        if self._grid_daily_state is None:
            self._grid_daily_state = DerivedDailyEnergyState()

        data, new_grid_state, _derived = apply_derived_daily_grid_energy(
            data,
            local_date=local_date,
            state=self._grid_daily_state,
            untrusted=self._untrusted_grid_counters(data, elapsed_seconds),
        )
        if new_grid_state.to_store() != self._grid_daily_state.to_store():
            await self._grid_daily_store.async_save(new_grid_state.to_store())
        self._grid_daily_state = new_grid_state
        return data

    def _untrusted_grid_counters(self, data: dict[str, Any], elapsed_seconds: float | None) -> frozenset[str]:
        """Lifetime counters whose latest sample we refuse to derive from.

        A disconnected or failing smart meter makes the inverter answer these registers
        with garbage (mkaiser#692), and a step upwards has no other guard — the baseline
        logic only re-anchors on a decrease. Holding the counter back leaves the entity
        showing whatever the device itself reported rather than a spike that would land in
        the Energy dashboard.

        Warned once per counter so a persistently broken meter cannot flood the log
        (the same pattern as ``_device_refresh_warned``); the flag clears when the counter
        reads sanely again, so a later recurrence is reported too.
        """
        from .derived_daily import DERIVED_DAILY_COUNTER_PAIRS, implausible_counter_jump

        untrusted: set[str] = set()
        for total_code, _ in DERIVED_DAILY_COUNTER_PAIRS:
            point = data.get(total_code)
            total = None if not isinstance(point, dict) else point.get("value")
            baseline = self._grid_daily_state.baselines.get(total_code) if self._grid_daily_state else None
            previous = baseline.last_total if baseline else None
            if isinstance(total, (int, float)) and implausible_counter_jump(previous, float(total), elapsed_seconds):
                untrusted.add(total_code)
                if total_code not in self._grid_glitch_warned:
                    self._grid_glitch_warned.add(total_code)
                    _LOGGER.warning(
                        "Ignoring %s on %s: it jumped to %s from %s, which no grid connection "
                        "could supply. Check the smart meter wiring; the derived daily figure "
                        "is held until it reads sanely again (#471)",
                        total_code,
                        self.plant_name,
                        total,
                        previous,
                    )
            else:
                self._grid_glitch_warned.discard(total_code)
        return frozenset(untrusted)

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
        except (SungrowModbusError, TimeoutError) as err:  # best-effort diagnostic
            _LOGGER.debug("daily_yield diagnostic capture failed for %s: %s", self.plant_name, err)

    def _within_availability_grace(self) -> bool:
        """True while the last successful poll is recent enough to keep serving stale data."""
        if self._last_successful_update is None:
            return False
        return (self.hass.loop.time() - self._last_successful_update) < AVAILABILITY_GRACE_SECONDS

    def _adjust_poll_backoff(self, *, rate_limited: bool, retry_after: float | None = None) -> None:
        """Back off the poll interval on rate-limit errors, restoring it on recovery (#156).

        Without a server hint each rate-limited poll doubles the interval up to
        ``BACKOFF_MAX_INTERVAL``, so the integration stops hammering iSolarCloud once it hits
        the hourly/monthly quota; the next successful poll restores the user's configured
        interval.

        When iSolarCloud states a retry delay (#458) that is used instead of the guess. It is
        floored at the user's configured interval — a short hint is not an invitation to poll
        faster than they asked for — and still capped at ``BACKOFF_MAX_INTERVAL``, so a
        month-long quota hint cannot park the integration for a month when an hourly retry
        would pick the reset up sooner.
        """
        if rate_limited:
            current = self.update_interval or self._base_update_interval
            if retry_after is None:
                new = min(current * 2, BACKOFF_MAX_INTERVAL)
            else:
                new = min(max(timedelta(seconds=retry_after), self._base_update_interval), BACKOFF_MAX_INTERVAL)
            if new != self.update_interval:
                self.update_interval = new
                _LOGGER.warning(
                    "iSolarCloud rate-limited %s; backing off poll interval to %s%s",
                    self.plant_name,
                    new,
                    " (server-suggested)" if retry_after is not None else "",
                )
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

    def _record_api_call(self, call_type: str) -> None:
        """Count one outbound iSolarCloud call against the rate tracker (#434).

        No-op when there is no tracker (a Modbus-only entry makes no API calls).
        """
        if self.rate_tracker is not None:
            self.rate_tracker.record(call_type)

    def record_api_call(self, call_type: str) -> None:
        """Public hook so non-poll cloud calls (dispatch writes) count too (#434).

        The dispatch entities (number/select) share this coordinator and write via the
        cloud ``Control``/``UserControl`` client; recording here keeps those calls in the
        same per-entry budget. On a Modbus-only entry the tracker is ``None`` so a local
        holding-register write is correctly not counted.
        """
        self._record_api_call(call_type)

    def _rate_issue_id(self) -> str:
        """Return the per-entry issue id for the proactive rate Repair (#434).

        Keyed on the config entry, not the plant, because the budget is per account and
        the tracker is shared across the entry's plant coordinators — one Repair per
        entry, not one per plant.
        """
        entry = self.config_entry
        entry_id = entry.entry_id if entry is not None else self.plant_id
        return f"{_RATE_WARNING_ISSUE}_{entry_id}"

    def _async_check_rate_budget(self) -> None:
        """Warn (log + Repair) as the observed API rate approaches the budget (#434).

        Evaluated lazily after a successful cloud poll — no timer or task. When the
        trailing-hour rate reaches the warning threshold it logs once per crossing and
        raises a per-entry Repair naming the dominant call type; when the rate falls back
        below the threshold the Repair is cleared. This is advisory only: it never
        throttles or fails the poll (auto-stretching the interval is deferred to a
        follow-up — see #434).
        """
        tracker = self.rate_tracker
        if tracker is None:
            return
        issue_id = self._rate_issue_id()
        if tracker.is_approaching_budget():
            observed = int(round(tracker.observed_rate_per_hour()))
            dominant = tracker.dominant_type()
            dominant_label = label_for(dominant) if dominant is not None else "polling"
            if not tracker.warning_active:
                tracker.warning_active = True
                _LOGGER.warning(
                    "Sungrow is making ~%d iSolarCloud calls/hour, approaching the ~%d/hour budget; "
                    "the %s dominates. Increase the polling interval (or disable per-device sensors) "
                    "to avoid the API rejecting requests (E999).",
                    observed,
                    tracker.budget_per_hour,
                    dominant_label,
                )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_RATE_WARNING_ISSUE,
                translation_placeholders={
                    "observed": str(observed),
                    "budget": str(tracker.budget_per_hour),
                    "dominant": dominant_label,
                },
                learn_more_url=_REPAIR_LEARN_MORE,
            )
            self._rate_repair_raised = True
        elif tracker.warning_active or self._rate_repair_raised:
            tracker.warning_active = False
            self._rate_repair_raised = False
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
        assert self.plants_service is not None  # only reached on the cloud path
        try:
            async with asyncio.timeout(self._poll_timeout):
                self._record_api_call(CALL_TYPE_DEVICE_LIST)
                devices = await self.plants_service.async_get_plant_devices(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
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
                self._record_api_call(CALL_TYPE_PLANT_DETAIL)
                details = await self.plants_service.async_get_plant_details(self.plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
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

        Since pysolarcloud 0.18 the endpoint's two "nothing" outcomes are distinct, and
        this method treats them differently: ``DeviceEndpointUnavailable`` means the
        account/region has no per-device endpoint at all, so the type is remembered and
        skipped on later polls (#288), whereas an empty dict means the endpoint works but
        this device type reports no points *yet* and is retried (#405).
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
                    self._record_api_call(CALL_TYPE_DEVICE_REALTIME)
                    result = await self.plants_service.async_get_device_realtime(
                        self.plant_id,
                        device_type,
                        ps_key_list=ps_keys or None,
                        extra_measure_points=extra or None,
                    )
            except DeviceEndpointUnavailable as err:
                # The account/region has no per-device endpoint at all — a permanent
                # capability gap, so remember it and stop asking on future polls (#288).
                # This is the *only* outcome that blacklists the type: a bare empty
                # result now means "available, but nothing reported yet" and must stay
                # retryable, or one quiet poll would hide a device for the whole session
                # (#405).
                _LOGGER.debug("Per-device realtime unavailable for plant %s type %s: %s", self.plant_id, type_id, err)
                self._unsupported_device_types.add(type_id)
                continue
            except (PySolarCloudException, ClientError, TimeoutError) as err:
                _LOGGER.debug("Per-device realtime failed for plant %s type %s: %s", self.plant_id, type_id, err)
                continue
            if not result:
                # Available, but this device type reports no points right now. Don't
                # blacklist it — the point set can populate later (#405).
                continue
            for uuid, points in result.items():
                merged.setdefault(str(uuid), {}).update(points)
        return merged
