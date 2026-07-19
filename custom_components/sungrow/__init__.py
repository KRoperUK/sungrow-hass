"""The Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from pysolarcloud import PySolarCloudException, UserAuth, UserControl
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
    CONF_SERIAL,
    CONF_TRANSPORT,
    CONF_USER_ACCOUNT,
    CONF_USER_PASSWORD,
    DEFAULT_CONSOLE_URL,
    DEFAULT_HOST,
    DOMAIN,
    GATEWAY_CONSOLE_URLS,
    GATEWAYS,
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
from .heartbeat import (
    _stop_heartbeat,
)
from .heartbeat import (
    async_start_heartbeat as async_start_heartbeat,
)
from .heartbeat import (
    async_stop_heartbeat as async_stop_heartbeat,
)
from .migration import (
    _async_split_legacy_hybrid,
)
from .migration import (
    async_migrate_entry as async_migrate_entry,
)
from .oauth_view import SungrowAuthCallbackView
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SENSOR]
# Modbus-only: sensors + connectivity binary sensor + local active-power controls (#220).
# Setup and unload MUST use the same list — unloading a platform that was never set up
# fails the unload, which breaks options-change reload.
MODBUS_ONLY_PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]
# cloud_user has sensors plus dispatch (UserControl over the app/web API, #271). No
# binary sensors (device fault/connectivity come from the OAuth device list shape).
CLOUD_USER_PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.SELECT, Platform.SENSOR]


def _entry_platforms(entry: SungrowConfigEntry) -> list[Platform]:
    """Return the platforms this entry sets up (fewer for cloud-free entries, #159/#268)."""
    transport = entry.data.get(CONF_TRANSPORT)
    if transport == TRANSPORT_MODBUS_ONLY:
        return MODBUS_ONLY_PLATFORMS
    if transport == TRANSPORT_CLOUD_USER:
        return CLOUD_USER_PLATFORMS
    return PLATFORMS


# How long to wait for a single cloud call during entry setup before giving up
# and letting HA retry later (ConfigEntryNotReady). Fixed rather than tied to the
# poll interval because setup runs once and can afford to be patient.
SETUP_TIMEOUT = 60

_LOGGER = logging.getLogger(__name__)


# Dispatch client: OAuth ``Control``, user-account ``UserControl`` (#271), or local
# ``ModbusControl`` (#220). Imported lazily in setup for ModbusControl to avoid
# circular imports at module load.
type DispatchControl = Control | UserControl


@dataclass
class SungrowData:
    """Runtime data stored on the config entry (``entry.runtime_data``)."""

    coordinators: list[SungrowPlantCoordinator]
    # OAuth: ``Control``; cloud_user: ``UserControl`` (#271); modbus_only: ``ModbusControl``
    # when holding maps are available (#220); None only if local control cannot attach.
    control: DispatchControl | None
    devices: dict[str, list[dict[str, Any]]]
    # plant_id -> (stop_event, task) for the running EMS heartbeat loops.
    heartbeats: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = field(default_factory=dict)
    # None for a Modbus-only entry (backfill is cloud-only, Requirement 1.2).
    backfill: BackfillManager | None = None


type SungrowConfigEntry = ConfigEntry[SungrowData]


async def _async_dispatch_supported(control: DispatchControl, devices: list[dict[str, Any]]) -> bool:
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
    except (PySolarCloudException, ClientError, TimeoutError) as err:
        # Fail-open on typed transport failures. Cloud-only path here; the Modbus
        # dispatch probe has its own tighter catch further down (#350).
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
    except (PySolarCloudException, ClientError, TimeoutError) as err:
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
    # Only nest under a *real* cloud plant device. Falling back to ``serial`` invented a
    # via_device parent that does not exist and trips HA 2025.12 warnings (live SG3.6RS).
    via_plant_id = cloud_plant_id

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
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Release the WiNet-S socket before HA retries setup; otherwise orphan clients
        # exhaust the dongle's single-connection slot and reload never recovers.
        coordinator.close_modbus()
        raise
    # Derive whether battery-gated dispatch controls should surface from the family
    # resolved by first_refresh's device-type detection (falling back to the config's
    # model code). Only SH hybrids have batteries — SG string inverters must stay
    # gated off to avoid the charge/discharge footguns on PV-only units (#148, #331).
    from .model_capabilities import resolve_capabilities

    modbus_client = getattr(coordinator, "_modbus_client", None)
    resolved_model_code = getattr(modbus_client, "model", None) or model
    caps = resolve_capabilities(resolved_model_code)
    coordinator.has_battery = bool(caps.has_battery)

    control: DispatchControl | None = None
    modbus_client = getattr(coordinator, "_modbus_client", None)
    if modbus_client is not None:
        from .modbus_control import ModbusControl

        modbus_control = ModbusControl(modbus_client, family=getattr(modbus_client, "model", None))
        if modbus_control.supported_parameters:
            # Probe once; fail-open to True only if the map is non-empty and readable.
            from .modbus import SungrowModbusError
            from .modbus_control import ModbusControlError

            try:
                coordinator.dispatch_update_supported = await modbus_control.async_check_update_support(
                    str(inverter["uuid"])
                )
            except (SungrowModbusError, ModbusControlError, TimeoutError) as err:
                _LOGGER.debug("Modbus control support probe failed: %s", err)
                coordinator.dispatch_update_supported = False
            if coordinator.dispatch_update_supported:
                control = modbus_control  # type: ignore[assignment,unused-ignore]
            else:
                _LOGGER.info(
                    "Local Modbus control map not readable on %s; dispatch entities disabled",
                    host or serial,
                )

    entry.runtime_data = SungrowData(coordinators=[coordinator], control=control, devices={serial: [inverter]})
    # Local Modbus sensors now live on the single inverter device (which is nested under
    # a matching cloud plant when one exists). Remove any legacy synthetic local plant
    # anchor left over from earlier builds so the UI does not show two plant devices.
    _remove_synthetic_local_plant_device(hass, entry, serial)
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
            # listen_once auto-removes after fire; wrap so unload after start does not
            # raise "Unable to remove unknown job listener" (seen on dell-serve reloads).
            remove = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _async_recheck_nesting)

            @callback
            def _cancel_recheck() -> None:
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    remove()

            entry.async_on_unload(_cancel_recheck)

    return True


async def _async_setup_cloud_user(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up a cloud entry authenticated with a user account (email/password) (#268/#271).

    Uses the unofficial app/web API via ``UserAuth`` instead of the developer OAuth app.
    Discovers plants, maps realtime points, and attaches dispatch via ``UserControl``
    (same number/select entities and safety rails as OAuth when the device accepts writes).
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
    control_service: DispatchControl = UserControl(user_auth)

    try:
        async with asyncio.timeout(SETUP_TIMEOUT):
            plant_list = await user_auth.async_get_plants()
    except Exception as err:
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(f"iSolarCloud user-account login failed: {err}") from err
        raise ConfigEntryNotReady(f"Unable to reach iSolarCloud (user account): {err}") from err

    coordinators: list[SungrowPlantCoordinator] = []
    devices_by_plant: dict[str, list[dict[str, Any]]] = {}
    for plant_info in plant_list:
        plant_id = str(plant_info["ps_id"])
        plant_name = plant_info.get("ps_name") or f"Plant {plant_id}"

        # Device list powers dispatch targeting and battery gating (#271). Failures are
        # non-fatal — plant sensors still work; controls appear when devices are known.
        try:
            async with asyncio.timeout(SETUP_TIMEOUT):
                devices = await user_auth.async_get_devices(plant_id)
        except (PySolarCloudException, ClientError, TimeoutError) as err:
            _LOGGER.warning("Could not fetch devices for plant %s (user account): %s", plant_name, err)
            devices = []

        coordinator = SungrowPlantCoordinator(hass, entry, None, plant_id, plant_name, devices, user_auth=user_auth)
        try:
            await coordinator.async_config_entry_first_refresh()
        except ConfigEntryAuthFailed:
            raise
        except ConfigEntryNotReady as err:
            _LOGGER.warning("Plant %s failed its initial user-account refresh, skipping: %s", plant_name, err)
            continue

        coordinator.dispatch_update_supported = await _async_dispatch_supported(control_service, devices)
        # User API has no design_capacity_battery plant-detail field in this path; gate
        # battery-only controls on ESS/battery device presence (same fail-open default).
        coordinator.has_battery = _has_battery_device(devices) if devices else True
        devices_by_plant[plant_id] = devices
        coordinators.append(coordinator)

    if plant_list and not coordinators:
        raise ConfigEntryNotReady("No plants could be set up; all initial refreshes failed")

    entry.runtime_data = SungrowData(
        coordinators=coordinators,
        control=control_service,
        devices=devices_by_plant,
    )

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

    # cloud_only path. Legacy cloud_modbus entries have been migrated to cloud_only
    # by ``async_migrate_entry`` (#348); older hybrid-shape entries with orphan
    # ``modbus_host`` in options get split into pure cloud + separate local here.
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
    # Pre-0.15.0 pysolarcloud annotated ``Auth.tokens`` as ``None``; 0.15.0 fixed it to
    # ``dict[str, Any] | None``. The ``unused-ignore`` on the ignore code keeps this
    # working on both the older and newer library type shapes.
    auth.tokens = dict(entry.data["tokens"])  # type: ignore[assignment,unused-ignore]

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
        # Free the WiNet-S Modbus TCP slot so options reload / reconfigure can reconnect.
        for coordinator in entry.runtime_data.coordinators:
            close_modbus = getattr(coordinator, "close_modbus", None)
            if close_modbus is not None:
                close_modbus()
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
