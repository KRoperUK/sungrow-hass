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
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.http import HomeAssistantView
from pysolarcloud.control import Control
from pysolarcloud.plants import DeviceType, Plants

from .auth import SungrowAuth
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
    DEFAULT_CONSOLE_URL,
    DEFAULT_HOST,
    DOMAIN,
    GATEWAY_CONSOLE_URLS,
    GATEWAYS,
    POINT_DEVICE_TYPE,
    TRANSPORT_MODBUS_ONLY,
)
from .coordinator import SungrowPlantCoordinator, describe_api_error, is_auth_error

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SENSOR]
# A cloud-free Modbus-only entry has no device status or dispatch, so it sets up only
# the sensor platform (#159). Setup and unload MUST use the same list — unloading a
# platform that was never set up fails the unload, which breaks the options-change
# reload and takes every entity unavailable.
MODBUS_ONLY_PLATFORMS: list[Platform] = [Platform.SENSOR]


def _entry_platforms(entry: SungrowConfigEntry) -> list[Platform]:
    """Return the platforms this entry sets up (fewer for a Modbus-only entry, #159)."""
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS_ONLY:
        return MODBUS_ONLY_PLATFORMS
    return PLATFORMS


# How long to wait for a heartbeat loop to observe its stop event and exit
# before force-cancelling it.
HEARTBEAT_STOP_TIMEOUT = 10

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


type SungrowConfigEntry = ConfigEntry[SungrowData]


def _matches_device_type(device: dict[str, Any], target: DeviceType) -> bool:
    """Return True if a discovered device is of ``target`` type.

    pysolarcloud converts a *known* device type to a ``DeviceType`` enum, but the
    raw API (and test mocks) may present it as an int or a string, so match against
    all three representations rather than a single one.
    """
    dt = device.get("device_type")
    return dt in (target, target.value, target.name)


def select_dispatch_device(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the device to attach dispatch (number/select) entities to.

    Only inverters and energy-storage systems accept charge/discharge dispatch, so
    ignore any other discovered devices (meters, EV chargers, ...). Prefer an ESS,
    then fall back to an inverter. Returns ``None`` when neither is present.
    """
    ess = [d for d in devices if _matches_device_type(d, DeviceType.ENERGY_STORAGE_SYSTEM)]
    if ess:
        return ess[0]
    inverters = [d for d in devices if _matches_device_type(d, DeviceType.INVERTER)]
    return inverters[0] if inverters else None


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


def resolve_point_device(point_code: str, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single physical device a plant point belongs to, else None (=plant).

    Re-homes a flat plant sensor onto its device (#158) only when the plant has exactly
    one device of a mapped type (the "singular" rule); 0 or >1 matches keep the point on
    the plant device so genuine aggregates (e.g. total power on a 2-inverter plant) stay
    correct. Codes with no mapping also stay on the plant.
    """
    types = POINT_DEVICE_TYPE.get(point_code)
    if not types:
        return None
    matches = [d for d in devices if d.get("uuid") and any(_matches_device_type(d, t) for t in types)]
    return matches[0] if len(matches) == 1 else None


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


def build_device_info(device: dict[str, Any], plant_id: str, *, fallback_name: str | None = None) -> DeviceInfo:
    """Build a device-registry entry for a physical device, nested under its plant.

    Enriches the HA device card with the model, serial number and manufacturer the
    cloud reports (``device_model_code`` / ``device_sn`` / ``factory_name`` from
    ``getDeviceListByPsId``) instead of a bare name, and links it to the plant device
    via ``via_device``. The uuid is stringified so the identifier matches
    ``_known_device_ids`` (which keys on ``str(uuid)``) and the device isn't pruned.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, str(device["uuid"]))},
        name=device.get("device_name") or device.get("device_model_name") or fallback_name,
        manufacturer=device.get("factory_name") or "Sungrow",
        model=device.get("device_model_code") or device.get("device_model_name"),
        serial_number=device.get("device_sn"),
        via_device=(DOMAIN, plant_id),
    )


def build_plant_device_info(plant_id: str, plant_name: str, console_url: str) -> DeviceInfo:
    """Build the plant "service" DeviceInfo that anchors the per-device ``via_device`` tree.

    Registered explicitly at setup and used as the fallback for any plant sensor that does
    not re-home onto a physical device (#158), so the plant device always exists as the
    parent even when every sensor moves onto an inverter/battery/meter.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, plant_id)},
        name=plant_name,
        manufacturer="Sungrow",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=console_url,
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
    return True


async def _async_setup_modbus_only(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up a cloud-free entry that reads a single inverter over local Modbus (#159).

    Created by zeroconf discovery of a WiNet-S: no credentials, no cloud calls. One
    coordinator reads the inverter's registers and the sensor platform builds entities
    from the same measure-point codes the cloud path uses.
    """
    serial = str(entry.data.get(CONF_SERIAL) or entry.unique_id or "inverter")
    model = str(entry.data.get(CONF_MODEL) or "Inverter")
    plant_name = f"Sungrow {model}"
    host = str(entry.options.get(CONF_MODBUS_HOST) or entry.data.get(CONF_MODBUS_HOST) or "")
    # One inverter device nested under a service "plant" anchor (mirrors the cloud
    # topology); distinct identifiers so the plant and inverter don't collide.
    inverter = {
        "uuid": f"{serial}_inv",
        "device_name": plant_name,
        "device_type": DeviceType.INVERTER,
        "device_model_code": model,
        "device_sn": serial,
        "factory_name": "SUNGROW",
    }
    coordinator = SungrowPlantCoordinator(hass, entry, None, serial, plant_name, [inverter])
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = SungrowData(coordinators=[coordinator], control=None, devices={serial: [inverter]})
    # Pre-create the plant service device (via_device parent) with the WiNet-S web UI
    # as its configuration URL.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **build_plant_device_info(serial, plant_name, f"http://{host}" if host else DEFAULT_CONSOLE_URL),
    )
    # Only the sensor platform: a Modbus-only entry has no cloud device status or dispatch.
    await hass.config_entries.async_forward_entry_setups(entry, _entry_platforms(entry))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up Sungrow iSolarCloud from a config entry."""
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS_ONLY:
        return await _async_setup_modbus_only(hass, entry)
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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload the platforms first: only tear down the heartbeats if that succeeds. Stopping
    # them up front would strand an active Charge/Discharge dispatch without its keepalive
    # (the inverter times out of External-EMS mode) while the entry stays LOADED on a
    # failed unload.
    unloaded = await hass.config_entries.async_unload_platforms(entry, _entry_platforms(entry))
    if unloaded:
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


async def async_start_heartbeat(
    hass: HomeAssistant, entry: SungrowConfigEntry, plant_id: str, device_uuid: str, interval: int
) -> None:
    """Start (or restart) the EMS heartbeat loop for a plant/device."""
    data = entry.runtime_data
    heartbeats = data.heartbeats

    stop_event = asyncio.Event()
    control = data.control
    assert control is not None  # the heartbeat is only ever started on a cloud dispatch entry
    # Tracked by the config entry so HA cancels it automatically on unload.
    task = entry.async_create_background_task(
        hass,
        control.heartbeat_loop(device_uuid, interval, stop_event),
        name=f"sungrow-heartbeat-{plant_id}",
    )
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
