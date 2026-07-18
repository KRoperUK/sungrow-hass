"""The Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.http import HomeAssistantView
from pysolarcloud import UserAuth
from pysolarcloud.control import Control
from pysolarcloud.plants import DeviceType, Plants

from .auth import SungrowAuth
from .backfill import BackfillManager
from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    CONF_USER_ACCOUNT,
    CONF_USER_PASSWORD,
    DEFAULT_CONSOLE_URL,
    DEFAULT_HOST,
    DOMAIN,
    GATEWAY_CONSOLE_URLS,
    GATEWAYS,
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_CLOUD_USER,
    TRANSPORT_MODBUS_ONLY,
)
from .coordinator import SungrowPlantCoordinator, describe_api_error, is_auth_error
from .device_helpers import (
    _matches_device_type,
    find_related_cloud_plant_id,
)
from .device_helpers import (
    build_device_info as build_device_info,
)
from .device_helpers import (
    build_plant_device_info as build_plant_device_info,
)
from .device_helpers import (
    resolve_point_device as resolve_point_device,
)
from .device_helpers import (
    select_dispatch_device as select_dispatch_device,
)
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SENSOR]
# A cloud-free Modbus-only entry has no cloud device status or dispatch, but it does
# get a local connectivity binary sensor (#159). Setup and unload MUST use the same
# list — unloading a platform that was never set up fails the unload, which breaks the
# options-change reload and takes every entity unavailable.
MODBUS_ONLY_PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]
# A cloud user-account entry has no dispatch (no Control) and, in Phase 2, no realtime
# points yet — only the sensor platform is forwarded; data lands in Phase 3 (#268/#269).
CLOUD_USER_PLATFORMS: list[Platform] = [Platform.SENSOR]


def _entry_platforms(entry: SungrowConfigEntry) -> list[Platform]:
    """Return the platforms this entry sets up (fewer for cloud-free entries, #159/#268)."""
    transport = entry.data.get(CONF_TRANSPORT)
    if transport == TRANSPORT_MODBUS_ONLY:
        return MODBUS_ONLY_PLATFORMS
    if transport == TRANSPORT_CLOUD_USER:
        return CLOUD_USER_PLATFORMS
    return PLATFORMS


# How long to wait for a heartbeat loop to observe its stop event and exit
# before force-cancelling it.
HEARTBEAT_STOP_TIMEOUT = 10

# Repair raised when the EMS heartbeat loop stops unexpectedly while a forced
# charge/discharge is active (#231/#254). The loop keeps the inverter in External-EMS
# mode; if it dies silently the inverter times out of forced mode and the command
# stops being applied, with nothing surfaced to the user until now.
_HEARTBEAT_STOPPED_ISSUE = "heartbeat_stopped"
_REPAIR_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"

# How long to wait for a single cloud call during entry setup before giving up
# and letting HA retry later (ConfigEntryNotReady). Fixed rather than tied to the
# poll interval because setup runs once and can afford to be patient.
SETUP_TIMEOUT = 60

_LOGGER = logging.getLogger(__name__)


@dataclass
class SungrowData:
    """Runtime data stored on the config entry (``entry.runtime_data``)."""

    coordinators: list[SungrowPlantCoordinator]
    # None for a Modbus-only (cloud-free) entry, which has no dispatch/control (#159).
    control: Control | None
    devices: dict[str, list[dict[str, Any]]]
    # plant_id -> (stop_event, task) for the running EMS heartbeat loops.
    heartbeats: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = field(default_factory=dict)
    # None for a Modbus-only entry (backfill is cloud-only, Requirement 1.2).
    backfill: BackfillManager | None = None


type SungrowConfigEntry = ConfigEntry[SungrowData]


async def _async_dispatch_supported(control: Control, devices: list[dict[str, Any]]) -> bool:
    """Return whether the plant's dispatch device accepts parameter writes.

    Fail-open: returns True unless the API explicitly reports the device does not
    support updates, so a transient check failure — or a dispatch device that only
    appears after setup — never hides working controls.
    """
    target = select_dispatch_device(devices)
    if target is None or not target.get("uuid"):
        return True
    try:
        return bool(await control.async_check_update_support(str(target["uuid"])))
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Could not check dispatch support for %s: %s", target.get("uuid"), err)
        return True


def _has_battery_device(devices: list[dict[str, Any]]) -> bool:
    """Return True if the plant reports an energy-storage or battery device."""
    return any(
        _matches_device_type(d, DeviceType.ENERGY_STORAGE_SYSTEM) or _matches_device_type(d, DeviceType.BATTERY)
        for d in devices
    )


async def _async_has_battery(plants_service: Plants, plant_id: str, devices: list[dict[str, Any]]) -> bool:
    """Return whether a plant has a battery, to gate battery-only dispatch controls (#148).

    The plant's configured battery capacity (``design_capacity_battery`` from the
    plant-detail endpoint) is authoritative: a value of 0 means a PV-only system, so
    the battery-only controls are hidden even if a hybrid inverter also reports an
    ESS device with no pack attached. Only when plant details can't be fetched (or
    omit the field) does this fall back to ESS/battery device presence, so a
    transient failure never hides a real battery user's controls.
    """
    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            details = await plants_service.async_get_plant_details(plant_id)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Could not fetch plant details for %s; using device list for battery check: %s", plant_id, err)
        return _has_battery_device(devices)
    for entry in details or []:
        capacity = entry.get("design_capacity_battery")
        if capacity is not None:
            try:
                return float(capacity) > 0
            except (TypeError, ValueError):
                break
    # No usable capacity figure — fall back to device presence.
    return _has_battery_device(devices)


def _remove_synthetic_local_plant_device(hass: HomeAssistant, entry: SungrowConfigEntry, serial: str) -> None:
    """Remove a stale synthetic local-plant anchor device created before a cloud plant existed.

    When the local Modbus entry sets up before the cloud entry is ready, it creates a
    synthetic plant service device keyed by the inverter serial. Once the cloud plant
    appears and we nest under it, that synthetic device is orphaned and should be
    removed so the UI does not show two plant-level devices for the same inverter.
    """
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if device.entry_type != dr.DeviceEntryType.SERVICE:
            continue
        for domain_key, ident in device.identifiers:
            if domain_key == DOMAIN and str(ident) == serial:
                registry.async_remove_device(device.id)
                _LOGGER.debug("Removed synthetic local plant device %s for serial %s", device.id, serial)
                return


_HYBRID_OPTION_KEYS = frozenset(
    {
        CONF_MODBUS_HOST,
        "modbus_port",
        "modbus_unit",
        "modbus_debug_daily_yield",
    }
)


def _async_split_legacy_hybrid(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Strip hybrid Modbus settings from a cloud entry and spawn local entries.

    Older builds stored ``modbus_host`` on the cloud entry and merged values. That
    mashup is gone: cloud stays pure, and a separate Modbus-only entry is created
    per known inverter serial when possible.
    """
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS_ONLY:
        return
    # cloud_modbus entries legitimately carry modbus_host — don't strip it (#216).
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_CLOUD_MODBUS:
        return
    host = str(entry.options.get(CONF_MODBUS_HOST) or entry.data.get(CONF_MODBUS_HOST) or "").strip()
    has_hybrid_keys = any(k in entry.options or k in entry.data for k in _HYBRID_OPTION_KEYS)
    if not host and not has_hybrid_keys:
        return

    debug = bool(entry.options.get("modbus_debug_daily_yield", False))
    new_options = {k: v for k, v in entry.options.items() if k not in _HYBRID_OPTION_KEYS}
    new_data = {k: v for k, v in entry.data.items() if k not in _HYBRID_OPTION_KEYS}
    if new_options != dict(entry.options) or new_data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
        _LOGGER.info(
            "Removed hybrid Modbus settings from cloud entry %s; use a separate local entry instead",
            entry.title,
        )

    if not host:
        return

    registry = dr.async_get(hass)
    serials: list[tuple[str, str]] = []
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        sn = device.serial_number
        if not sn:
            continue
        model = device.model or "Inverter"
        serials.append((sn, str(model)))
    # de-dupe by serial, keep first model
    seen: set[str] = set()
    unique_serials: list[tuple[str, str]] = []
    for sn, model in serials:
        if sn in seen:
            continue
        seen.add(sn)
        unique_serials.append((sn, model))

    if not unique_serials:
        _LOGGER.warning(
            "Cloud entry %s had modbus_host=%s but no inverter serial in the device registry; "
            "set up local Modbus via discovery (or Add Integration) for a separate local entry",
            entry.title,
            host,
        )
        return

    from homeassistant.config_entries import SOURCE_IMPORT

    for serial, model in unique_serials:
        unique_id = f"modbus_{serial}"
        if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id) is not None:
            continue
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                    CONF_SERIAL: serial,
                    CONF_MODEL: model,
                    CONF_MODBUS_HOST: host,
                    CONF_SCAN_INTERVAL: 30,
                    "modbus_debug_daily_yield": debug,
                },
            )
        )
        _LOGGER.info(
            "Created separate local Modbus entry for serial %s (host %s) split from cloud entry %s",
            serial,
            host,
            entry.title,
        )


class IterableSchema(vol.Schema):
    """A Schema that can be iterated over (yielding nothing) to satisfy HA's checks."""

    def __iter__(self) -> Iterator[Any]:
        """Return an empty iterator."""
        return iter([])

    def __contains__(self, item: object) -> bool:
        """Return False for any item check."""
        return False


# Workaround for HA 2025.2+ treating the schema function/object as the config dict
CONFIG_SCHEMA = IterableSchema({}, extra=vol.ALLOW_EXTRA)


def _ensure_callback_view_registered(hass: HomeAssistant) -> None:
    """Register the OAuth callback HTTP view exactly once.

    iSolarCloud redirects back to ``/api/sungrow_hass/callback`` *during* the very
    first config flow — before any config entry exists, and therefore before
    ``async_setup`` (which used to be the only place the view was registered) has
    run. Without this, a first-time install gets a 404 on the callback while an
    install that already has a Sungrow entry works. Registering here, guarded by a
    flag so aiohttp never sees a duplicate route, fixes the first-time case.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("callback_view_registered"):
        return
    hass.http.register_view(SungrowAuthCallbackView())
    domain_data["callback_view_registered"] = True


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Sungrow iSolarCloud component."""
    _ensure_callback_view_registered(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry versions to the current schema."""
    if config_entry.version == 1:
        # v1 stored scan_interval in minutes; v2 uses seconds.
        old_interval = config_entry.options.get(CONF_SCAN_INTERVAL, 5)
        new_options = {**config_entry.options, CONF_SCAN_INTERVAL: old_interval * 60}
        hass.config_entries.async_update_entry(config_entry, options=new_options, version=2)
        _LOGGER.info("Migrated scan_interval from %d minutes to %d seconds", old_interval, old_interval * 60)

    if config_entry.version == 2:
        # v2→v3: back-fill transport field for existing entries.
        new_data = dict(config_entry.data)
        if CONF_TRANSPORT not in new_data:
            new_data[CONF_TRANSPORT] = TRANSPORT_CLOUD_ONLY
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)
        _LOGGER.info(
            "Migrated config entry %s to version 3 (transport=%s)", config_entry.title, new_data[CONF_TRANSPORT]
        )

    # Defensive back-fill: ensure cloud entries carry app_id (#245).
    # The unique_id for cloud entries IS the app_id (set during initial setup).
    # If the data key was lost (corrupt storage, partial migration, older RC builds)
    # we can recover it from unique_id. Kept here as a safety net even though
    # async_setup_entry also back-fills, since migration runs before setup.
    if config_entry.version >= 3:
        data = dict(config_entry.data)
        transport = data.get(CONF_TRANSPORT)
        if transport != TRANSPORT_MODBUS_ONLY and not data.get(CONF_APP_ID):
            uid = config_entry.unique_id
            if uid and not uid.startswith("modbus_"):
                data[CONF_APP_ID] = uid
                hass.config_entries.async_update_entry(config_entry, data=data)
                _LOGGER.info(
                    "Back-filled missing app_id from unique_id for entry %s",
                    config_entry.title,
                )

    return True


async def _async_setup_modbus_only(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up a cloud-free entry that reads a single inverter over local Modbus (#159).

    Created by zeroconf discovery or import from a legacy hybrid split: no credentials,
    no cloud calls. One coordinator reads the inverter's registers and the sensor
    platform builds entities from the same measure-point codes the cloud path uses.

    When a cloud entry already owns this serial, the local inverter is nested under
    that cloud plant (``via_device``) without merging any sensor values.
    """
    serial = str(entry.data.get(CONF_SERIAL) or entry.unique_id or "inverter")
    # unique_id is modbus_{serial}; strip prefix if present for a clean serial key
    if serial.startswith("modbus_"):
        serial = serial.removeprefix("modbus_")
    model = str(entry.data.get(CONF_MODEL) or "Inverter")
    local_name = f"{model} (local)"
    host = str(entry.options.get(CONF_MODBUS_HOST) or entry.data.get(CONF_MODBUS_HOST) or "")
    winet_url = f"http://{host}" if host else None
    cloud_plant_id = find_related_cloud_plant_id(hass, serial)
    via_plant_id = cloud_plant_id or serial

    # One inverter device. Distinct identifiers so the plant and inverter don't collide.
    inverter = {
        "uuid": f"{serial}_inv",
        "device_name": local_name,
        "device_type": DeviceType.INVERTER,
        "device_model_code": model,
        "device_sn": serial,
        "factory_name": "SUNGROW",
    }
    coordinator = SungrowPlantCoordinator(hass, entry, None, serial, local_name, [inverter])
    coordinator.via_plant_id = via_plant_id
    coordinator.local_configuration_url = winet_url
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SungrowData(coordinators=[coordinator], control=None, devices={serial: [inverter]})
    # Local Modbus sensors now live on the single inverter device (which is nested under
    # a matching cloud plant when one exists). Remove any legacy synthetic local plant
    # anchor left over from earlier builds so the UI does not show two plant devices.
    _remove_synthetic_local_plant_device(hass, entry, serial)
    # Only the sensor platform: a Modbus-only entry has no cloud device status or dispatch.
    await hass.config_entries.async_forward_entry_setups(entry, _entry_platforms(entry))

    # If no cloud plant was found at setup time, the local entry may have set up before
    # the cloud entry. Re-check once HA is running and reload so the inverter can nest
    # under the cloud plant device. If HA is already running (e.g. config flow addition),
    # schedule the check after a short delay so any in-progress cloud setups finish first.
    if cloud_plant_id is None:

        async def _async_recheck_nesting(_: Any) -> None:
            if find_related_cloud_plant_id(hass, serial):
                _LOGGER.debug("Cloud plant found after startup for %s; reloading local entry", serial)
                await hass.config_entries.async_reload(entry.entry_id)

        if hass.is_running:
            entry.async_on_unload(async_call_later(hass, 5, _async_recheck_nesting))
        else:
            entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_recheck_nesting))

    return True


async def _async_setup_cloud_user(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up a cloud entry authenticated with a user account (email/password) (#268).

    Uses the unofficial app/web API via ``UserAuth`` instead of the developer OAuth app.
    Phase 2 authenticates, discovers plants and registers each plant device; realtime
    sensor data is Phase 3 (#269), so the coordinator produces no measure points yet.
    Isolated from the OAuth path so the unofficial transport can't destabilise it.
    """
    session = async_get_clientsession(hass)
    host = GATEWAYS.get(entry.data.get(CONF_GATEWAY, ""), DEFAULT_HOST)
    user_auth = UserAuth(
        host,
        entry.data[CONF_USER_ACCOUNT],
        entry.data[CONF_USER_PASSWORD],
        websession=session,
    )

    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            plant_list = await user_auth.async_get_plants()
    except Exception as err:
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(f"iSolarCloud user-account login failed: {err}") from err
        raise ConfigEntryNotReady(f"Unable to reach iSolarCloud (user account): {err}") from err

    coordinators: list[SungrowPlantCoordinator] = []
    for plant_info in plant_list:
        plant_id = str(plant_info["ps_id"])
        plant_name = plant_info.get("ps_name") or f"Plant {plant_id}"
        coordinator = SungrowPlantCoordinator(hass, entry, None, plant_id, plant_name, user_auth=user_auth)
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryAuthFailed:
            raise
        except ConfigEntryNotReady as err:
            _LOGGER.warning("Plant %s failed its initial user-account refresh, skipping: %s", plant_name, err)
            continue
        coordinators.append(coordinator)

    if plant_list and not coordinators:
        raise ConfigEntryNotReady("No plants could be set up; all initial refreshes failed")

    # No Control client: dispatch is not supported over the user-account API in Phase 2.
    entry.runtime_data = SungrowData(coordinators=coordinators, control=None, devices={})

    console_url = GATEWAY_CONSOLE_URLS.get(entry.data.get(CONF_GATEWAY, ""), DEFAULT_CONSOLE_URL)
    device_registry = dr.async_get(hass)
    for coordinator in coordinators:
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **build_plant_device_info(coordinator.plant_id, coordinator.plant_name, console_url),
        )

    await hass.config_entries.async_forward_entry_setups(entry, _entry_platforms(entry))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up Sungrow iSolarCloud from a config entry."""
    transport = entry.data.get(CONF_TRANSPORT)

    if transport is None:
        _LOGGER.warning("Config entry %s missing CONF_TRANSPORT; defaulting to cloud_only", entry.title)
        transport = TRANSPORT_CLOUD_ONLY

    if transport == TRANSPORT_MODBUS_ONLY:
        return await _async_setup_modbus_only(hass, entry)

    if transport == TRANSPORT_CLOUD_USER:
        return await _async_setup_cloud_user(hass, entry)

    if transport == TRANSPORT_CLOUD_MODBUS:
        _LOGGER.info(
            "Entry %s configured for cloud+modbus; Modbus wiring deferred to #217",
            entry.title,
        )

    # cloud_only and cloud_modbus both use the standard cloud coordinator path.
    # Split legacy hybrid cloud+Modbus entries into pure cloud + separate local.
    _async_split_legacy_hybrid(hass, entry)

    # Defensive back-fill: recover app_id from unique_id if missing (#245).
    # async_migrate_entry only runs on version mismatch, so entries already at v3
    # skip migration entirely — this catches them at load time.
    if not entry.data.get(CONF_APP_ID):
        uid = entry.unique_id
        if uid and not uid.startswith("modbus_"):
            new_data = dict(entry.data)
            new_data[CONF_APP_ID] = uid
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info("Back-filled missing app_id from unique_id for entry %s", entry.title)
        else:
            raise ConfigEntryAuthFailed("Missing app_id; reconfigure the entry to supply it")

    if "tokens" not in entry.data:
        # Nothing to authenticate with — ask the user to re-authorize.
        raise ConfigEntryAuthFailed("No stored tokens; re-authorization required")

    session = async_get_clientsession(hass)
    host = GATEWAYS.get(entry.data[CONF_GATEWAY], DEFAULT_HOST)

    def _save_tokens(tokens: dict[str, Any]) -> None:
        """Persist refreshed/rotated tokens back to the config entry."""
        hass.config_entries.async_update_entry(entry, data={**entry.data, "tokens": tokens})

    auth = SungrowAuth(
        host=host,
        appkey=entry.data[CONF_APP_KEY],
        access_key=entry.data[CONF_APP_SECRET],
        app_id=entry.data[CONF_APP_ID],
        websession=session,
        token_updater=_save_tokens,
    )
    # Restore previously stored tokens (a copy so we never mutate entry.data in place).
    # pysolarcloud annotates Auth.tokens as None, but it holds a dict at runtime.
    auth.tokens = dict(entry.data["tokens"])  # type: ignore[assignment]

    plants_service = Plants(auth)
    control_service = Control(auth)

    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            plant_list = await plants_service.async_get_plants()
    except Exception as err:
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(f"Authentication with iSolarCloud failed: {err}") from err
        # A timeout arrives here as TimeoutError and is treated as transient.
        raise ConfigEntryNotReady(describe_api_error(err) or f"Unable to fetch plants from iSolarCloud: {err}") from err

    coordinators: list[SungrowPlantCoordinator] = []
    devices_by_plant: dict[str, list[dict[str, Any]]] = {}
    for plant_info in plant_list:
        plant_id = str(plant_info["ps_id"])
        plant_name = plant_info["ps_name"]

        # Discover ALL devices first (not just inverter/ESS): dispatch filters down to
        # the dispatch-capable ones, while per-device sensors (issue #74) can use any
        # of them. Failures here are non-fatal — the plant still works on plant-level
        # data, just without device discovery.
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                devices = await plants_service.async_get_plant_devices(plant_id)
        except Exception as err:
            _LOGGER.warning("Could not fetch devices for plant %s: %s", plant_name, err)
            devices = []

        coordinator = SungrowPlantCoordinator(hass, entry, plants_service, plant_id, plant_name, devices)
        try:
            # Raises ConfigEntryAuthFailed (reauth) / ConfigEntryNotReady (retry) as
            # classified by the coordinator.
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryAuthFailed:
            # A dead credential is fatal for every plant (they share one login) —
            # propagate immediately so HA starts reauth instead of dropping a plant.
            raise
        except ConfigEntryNotReady as err:
            # One plant's transient failure must not abort setup of the others (#115);
            # it will be retried on the next poll once the entry is loaded.
            _LOGGER.warning("Plant %s failed its initial data refresh, skipping for now: %s", plant_name, err)
            continue

        coordinator.dispatch_update_supported = await _async_dispatch_supported(control_service, devices)
        coordinator.has_battery = await _async_has_battery(plants_service, plant_id, devices)
        devices_by_plant[plant_id] = devices
        coordinators.append(coordinator)

    if plant_list and not coordinators:
        # Every plant failed a transient refresh — retry the whole entry later.
        raise ConfigEntryNotReady("No plants could be set up; all initial data refreshes failed")

    entry.runtime_data = SungrowData(
        coordinators=coordinators,
        control=control_service,
        devices=devices_by_plant,
    )

    # Register the plant "service" device explicitly so it always exists as the
    # via_device parent — even when every plant sensor re-homes onto a physical
    # device (#158). Without this, a re-homed device references a non-existent
    # via_device (HA warns and breaks it in 2025.12).
    console_url = GATEWAY_CONSOLE_URLS.get(entry.data.get(CONF_GATEWAY, ""), DEFAULT_CONSOLE_URL)
    device_registry = dr.async_get(hass)
    for coordinator in coordinators:
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **build_plant_device_info(coordinator.plant_id, coordinator.plant_name, console_url),
        )

    await hass.config_entries.async_forward_entry_setups(entry, _entry_platforms(entry))

    # Prune devices that drop out of the API after each refresh (stale-devices).
    @callback
    def _prune() -> None:
        _async_prune_stale_devices(hass, entry)

    for coordinator in coordinators:
        entry.async_on_unload(coordinator.async_add_listener(_prune))

    # NB: we deliberately do NOT register an update listener here. Options changes
    # reload the entry via ``SungrowOptionsFlow`` (``OptionsFlowWithReload``); a bare
    # update listener would also fire on every token rotation (a plain ``entry.data``
    # write from ``_save_tokens``), needlessly reloading the whole integration (#110).

    # Backfill is cloud-only (Requirement 1.2): construct the manager and kick off the
    # automatic run in a background task so it never delays setup or the first realtime
    # poll (Requirement 1.1). The Modbus-only path never reaches here, so it never gets a
    # manager. The start is gated on Home Assistant being fully started so the historical
    # imports never race the recorder starting up (Requirements 5.4, 5.5).
    manager = BackfillManager(hass, entry)
    entry.runtime_data.backfill = manager

    # Register the on-demand sungrow.backfill service once (idempotent, Requirement 2.1).
    async_setup_services(hass)

    async def _async_start_backfill() -> None:
        if not hass.is_running:
            started = asyncio.Event()

            @callback
            def _on_started(_event: Any) -> None:
                started.set()

            entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started))
            await started.wait()
        await manager.async_start_automatic()

    entry.async_create_background_task(hass, _async_start_backfill(), name="sungrow-backfill-start")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload the platforms first: only tear down the heartbeats if that succeeds. Stopping
    # them up front would strand an active Charge/Discharge dispatch without its keepalive
    # (the inverter times out of External-EMS mode) while the entry stays LOADED on a
    # failed unload.
    unloaded = await hass.config_entries.async_unload_platforms(entry, _entry_platforms(entry))
    if unloaded:
        # Cancel any in-flight Backfill runs before tearing down (Requirement 1.5). Cloud
        # entries have a manager; the Modbus-only path leaves ``backfill`` as None.
        manager = entry.runtime_data.backfill
        if manager is not None:
            await manager.async_shutdown()
        heartbeats = entry.runtime_data.heartbeats
        for heartbeat in list(heartbeats.values()):
            await _stop_heartbeat(heartbeat)
        heartbeats.clear()
    return unloaded


def _known_device_ids(entry: SungrowConfigEntry) -> set[tuple[str, str]]:
    """Return the device-registry identifiers currently reported by the API.

    One per plant (the plant device) plus one per device the coordinators have
    seen on their latest poll. Used to distinguish live devices from stale ones.
    """
    known: set[tuple[str, str]] = set()
    for coordinator in entry.runtime_data.coordinators:
        known.add((DOMAIN, coordinator.plant_id))
        for device in coordinator.devices:
            uuid = device.get("uuid")
            if uuid:
                known.add((DOMAIN, str(uuid)))
    return known


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: SungrowConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deletion of a device the API no longer reports (stale-devices).

    Returns True (permit removal) only when none of the device's identifiers match
    a currently-known plant or device, so devices that are still present cannot be
    removed by accident.
    """
    known = _known_device_ids(config_entry)
    return not any(identifier in known for identifier in device_entry.identifiers)


@callback
def _async_prune_stale_devices(hass: HomeAssistant, entry: SungrowConfigEntry) -> None:
    """Remove device-registry entries the API no longer reports (stale-devices).

    Runs after each coordinator refresh. A device is removed only when none of its
    identifiers are in the freshly-fetched device list. Because a failed device
    fetch keeps the previous list (see the coordinator), a transient API outage
    can't trigger spurious removals.
    """
    known = _known_device_ids(entry)
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if any(identifier in known for identifier in device.identifiers):
            continue
        registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def _stop_heartbeat(heartbeat: tuple[asyncio.Event, asyncio.Task[None]]) -> None:
    """Signal a heartbeat loop to stop and wait for it to actually exit."""
    stop_event, task = heartbeat
    stop_event.set()
    try:
        async with asyncio.timeout(HEARTBEAT_STOP_TIMEOUT):
            await task
    except TimeoutError:
        _LOGGER.warning("Heartbeat loop did not stop within %ss; cancelling", HEARTBEAT_STOP_TIMEOUT)
        task.cancel()
    except asyncio.CancelledError:
        pass
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Heartbeat loop raised while stopping")


def _plant_name(entry: SungrowConfigEntry, plant_id: str) -> str:
    """Return the plant's display name for a Repair message, falling back to its id."""
    data = getattr(entry, "runtime_data", None)
    for coordinator in getattr(data, "coordinators", []) or []:
        if coordinator.plant_id == plant_id:
            return str(coordinator.plant_name)
    return plant_id


@callback
def _on_heartbeat_done(
    hass: HomeAssistant,
    entry: SungrowConfigEntry,
    plant_id: str,
    stop_event: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    """Detect an EMS heartbeat loop that stopped unexpectedly and raise a Repair (#254).

    The heartbeat keeps the inverter in External-EMS mode while a forced charge/discharge
    is active. If the loop raises and exits on its own — as seen in #231, where it died
    silently for ~1h48m — the inverter times out of forced mode and the command quietly
    stops being applied. A *requested* stop (``stop_event`` set) or a cancellation (entry
    unload / HA shutdown) is expected and ignored; anything else surfaces an actionable
    Repair so the user knows dispatch is no longer being kept alive.
    """
    if stop_event.is_set() or task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:  # pragma: no cover - guarded by task.cancelled() above
        return
    _LOGGER.error(
        "EMS heartbeat loop for plant %s stopped unexpectedly; a forced charge/discharge "
        "is no longer being kept alive on the inverter: %s",
        plant_id,
        exc,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{_HEARTBEAT_STOPPED_ISSUE}_{plant_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_HEARTBEAT_STOPPED_ISSUE,
        translation_placeholders={"plant": _plant_name(entry, plant_id)},
        learn_more_url=_REPAIR_LEARN_MORE,
    )


async def async_start_heartbeat(
    hass: HomeAssistant, entry: SungrowConfigEntry, plant_id: str, device_uuid: str, interval: int
) -> None:
    """Start (or restart) the EMS heartbeat loop for a plant/device."""
    data = entry.runtime_data
    heartbeats = data.heartbeats

    # A fresh keepalive is starting, so clear any stale "heartbeat stopped" Repair (#254).
    ir.async_delete_issue(hass, DOMAIN, f"{_HEARTBEAT_STOPPED_ISSUE}_{plant_id}")

    stop_event = asyncio.Event()
    control = data.control
    assert control is not None  # the heartbeat is only ever started on a cloud dispatch entry
    # Tracked by the config entry so HA cancels it automatically on unload.
    task = entry.async_create_background_task(
        hass,
        control.heartbeat_loop(device_uuid, interval, stop_event),
        name=f"sungrow-heartbeat-{plant_id}",
    )
    # Surface an unexpected exit (the #231 silent-death bug) as a Repair. A requested
    # stop or a cancellation is ignored by the callback.
    task.add_done_callback(lambda finished: _on_heartbeat_done(hass, entry, plant_id, stop_event, finished))
    # Publish the new loop into the map BEFORE awaiting the old one's stop, so two
    # concurrent starts can't interleave across that await and orphan a task (a task
    # left running but no longer in `heartbeats`). Whichever start runs last owns the
    # map; every task it displaces is stopped by the displacing call.
    existing = heartbeats.get(plant_id)
    heartbeats[plant_id] = (stop_event, task)
    if existing is not None:
        await _stop_heartbeat(existing)


async def async_stop_heartbeat(hass: HomeAssistant, entry: SungrowConfigEntry, plant_id: str) -> None:
    """Stop the EMS heartbeat loop for a plant."""
    # An intentional stop means a dead-heartbeat Repair (if any) no longer applies (#254).
    ir.async_delete_issue(hass, DOMAIN, f"{_HEARTBEAT_STOPPED_ISSUE}_{plant_id}")
    heartbeats = entry.runtime_data.heartbeats
    heartbeat = heartbeats.pop(plant_id, None)
    if heartbeat is not None:
        await _stop_heartbeat(heartbeat)


class SungrowAuthCallbackView(HomeAssistantView):
    """Sungrow Authorization Callback View."""

    requires_auth = False
    url = "/api/sungrow_hass/callback"
    name = "api:sungrow_hass:callback"

    @staticmethod
    def _resolve_future(
        flows: dict[str, asyncio.Future[str]], states: dict[str, str], flow_id: str | None, state: str | None
    ) -> asyncio.Future[str] | None:
        """Correlate a callback to its pending flow's future.

        Prefers an explicit ``flow_id``, then the OAuth ``state`` param. If either
        correlator is present but matches no known flow, returns ``None`` rather than
        guessing — a stale or foreign correlator must never be misrouted onto a
        different flow's future (#116). Only when NO correlator is supplied at all
        (iSolarCloud may strip query params from the redirect) does it fall back to
        the sole pending flow.
        """
        if flow_id and flow_id in flows:
            return flows.get(flow_id)
        if state and state in states:
            return flows.get(states[state])
        if flow_id or state:
            # A correlator was supplied but matched nothing: do not misroute it.
            return None
        if len(flows) == 1:
            return next(iter(flows.values()))
        return None

    async def get(self, request: web.Request) -> web.Response:
        """Handle callback from iSolarCloud after user authorization."""
        hass: HomeAssistant = request.app["hass"]
        params = request.query
        code = params.get("code")
        flow_id = params.get("flow_id")
        state = params.get("state")

        if not code:
            # Log only the parameter *names* — never their values. An external redirect
            # could carry sensitive values, and users are told to enable debug logging.
            _LOGGER.warning("Callback received but missing 'code'. Query params present: %s", list(params))
            return web.Response(text="Missing code parameter. Please try again.", status=400)

        # Never log the authorization code — it's a single-use credential that exchanges
        # for tokens (mirrors the rule in config_flow.async_step_finish). Presence is implied.
        _LOGGER.debug("Callback received with an authorization code (flow_id=%s, state=%s)", flow_id, state)

        # Signal the waiting future so the config flow's background task can
        # resume the flow cleanly.
        domain_data = hass.data.get(DOMAIN, {})
        flows = domain_data.get("flows", {})
        states = domain_data.get("states", {})
        future = self._resolve_future(flows, states, flow_id, state)
        if future is not None and not future.done():
            future.set_result(code)
            return web.Response(
                text="Authorization successful! You can close this window and return to Home Assistant.",
                content_type="text/html",
            )

        _LOGGER.warning(
            "OAuth callback received (flow_id=%s, state=%s) but no pending future was found", flow_id, state
        )
        return web.Response(
            text="Authorization request not found or already completed. Please return to Home Assistant and try again.",
            status=400,
        )
