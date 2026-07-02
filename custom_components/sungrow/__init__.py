"""The Sungrow iSolarCloud integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pysolarcloud.control import Control
from pysolarcloud.plants import DeviceType, Plants

from .auth import SungrowAuth
from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_SCAN_INTERVAL,
    DEFAULT_HOST,
    DOMAIN,
    GATEWAYS,
)
from .coordinator import SungrowPlantCoordinator, is_auth_error

PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.SELECT, Platform.SENSOR]

# How long to wait for a heartbeat loop to observe its stop event and exit
# before force-cancelling it.
HEARTBEAT_STOP_TIMEOUT = 10

_LOGGER = logging.getLogger(__name__)


@dataclass
class SungrowData:
    """Runtime data stored on the config entry (``entry.runtime_data``)."""

    coordinators: list[SungrowPlantCoordinator]
    control: Control
    devices: dict[str, list[dict]]
    # plant_id -> (stop_event, task) for the running EMS heartbeat loops.
    heartbeats: dict[str, tuple[asyncio.Event, asyncio.Task]] = field(default_factory=dict)


type SungrowConfigEntry = ConfigEntry[SungrowData]


def _matches_device_type(device: dict, target: DeviceType) -> bool:
    """Return True if a discovered device is of ``target`` type.

    pysolarcloud converts a *known* device type to a ``DeviceType`` enum, but the
    raw API (and test mocks) may present it as an int or a string, so match against
    all three representations rather than a single one.
    """
    dt = device.get("device_type")
    return dt in (target, target.value, target.name)


def select_dispatch_device(devices: list[dict]) -> dict | None:
    """Pick the device to attach dispatch (number/select) entities to.

    Prefer an energy-storage system — that's what accepts charge/discharge dispatch
    — and fall back to the first discovered device otherwise. Returns ``None`` when
    there are no devices.
    """
    if not devices:
        return None
    ess = [d for d in devices if _matches_device_type(d, DeviceType.ENERGY_STORAGE_SYSTEM)]
    return ess[0] if ess else devices[0]


class IterableSchema(vol.Schema):
    """A Schema that can be iterated over (yielding nothing) to satisfy HA's checks."""

    def __iter__(self):
        """Return an empty iterator."""
        return iter([])

    def __contains__(self, item):
        """Return False for any item check."""
        return False


# Workaround for HA 2025.2+ treating the schema function/object as the config dict
CONFIG_SCHEMA = IterableSchema({}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Sungrow iSolarCloud component."""
    hass.http.register_view(SungrowAuthCallbackView())
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


async def async_setup_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Set up Sungrow iSolarCloud from a config entry."""
    if "tokens" not in entry.data:
        # Nothing to authenticate with — ask the user to re-authorize.
        raise ConfigEntryAuthFailed("No stored tokens; re-authorization required")

    session = async_get_clientsession(hass)
    host = GATEWAYS.get(entry.data[CONF_GATEWAY], DEFAULT_HOST)

    def _save_tokens(tokens: dict) -> None:
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
    auth.tokens = dict(entry.data["tokens"])

    plants_service = Plants(auth)
    control_service = Control(auth)

    try:
        plant_list = await plants_service.async_get_plants()
    except Exception as err:
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(f"Authentication with iSolarCloud failed: {err}") from err
        raise ConfigEntryNotReady(f"Unable to fetch plants from iSolarCloud: {err}") from err

    coordinators: list[SungrowPlantCoordinator] = []
    devices_by_plant: dict[str, list[dict]] = {}
    for plant_info in plant_list:
        plant_id = str(plant_info["ps_id"])
        plant_name = plant_info["ps_name"]
        coordinator = SungrowPlantCoordinator(hass, entry, plants_service, plant_id, plant_name)
        # Raises ConfigEntryNotReady / ConfigEntryAuthFailed as classified by the coordinator.
        await coordinator.async_config_entry_first_refresh()
        coordinators.append(coordinator)

        # Discover dispatch-capable devices (inverters / ESS) for this plant. Failures here are
        # non-fatal — dispatch entities simply won't be created for this plant.
        try:
            devices = await plants_service.async_get_plant_devices(
                plant_id,
                device_types=[DeviceType.INVERTER, DeviceType.ENERGY_STORAGE_SYSTEM],
            )
            devices_by_plant[plant_id] = devices
        except Exception as err:
            _LOGGER.warning("Could not fetch devices for plant %s: %s", plant_name, err)
            devices_by_plant[plant_id] = []

    entry.runtime_data = SungrowData(
        coordinators=coordinators,
        control=control_service,
        devices=devices_by_plant,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when its options (e.g. scan interval) change.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> bool:
    """Unload a config entry."""
    heartbeats = entry.runtime_data.heartbeats
    for heartbeat in list(heartbeats.values()):
        await _stop_heartbeat(heartbeat)
    heartbeats.clear()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: SungrowConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _stop_heartbeat(heartbeat: tuple[asyncio.Event, asyncio.Task]) -> None:
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

    # Stop any existing loop for this plant and wait for it to exit before
    # starting a new one, so two loops never run for the same device.
    existing = heartbeats.pop(plant_id, None)
    if existing is not None:
        await _stop_heartbeat(existing)

    stop_event = asyncio.Event()
    control: Control = data.control
    # Tracked by the config entry so HA cancels it automatically on unload.
    task = entry.async_create_background_task(
        hass,
        control.heartbeat_loop(device_uuid, interval, stop_event),
        name=f"sungrow-heartbeat-{plant_id}",
    )
    heartbeats[plant_id] = (stop_event, task)


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

    async def get(self, request: web.Request) -> web.Response:
        """Handle callback from iSolarCloud after user authorization."""
        hass: HomeAssistant = request.app["hass"]
        params = request.query
        code = params.get("code")
        flow_id = params.get("flow_id")

        if not code:
            _LOGGER.warning("Callback received but missing code. Params: %s", params)
            return web.Response(text="Missing code parameter. Please try again.", status=400)

        _LOGGER.debug("Callback received with code: %s, flow_id: %s", code, flow_id)

        # Signal the waiting future so the config flow's background task can
        # resume the flow cleanly.
        flows = hass.data.get(DOMAIN, {}).get("flows", {})
        if flow_id:
            future = flows.get(flow_id)
        elif len(flows) == 1:
            # iSolarCloud doesn't preserve extra query params on the redirect URI,
            # so flow_id may be absent — fall back to the only pending flow.
            future = next(iter(flows.values()))
        else:
            future = None
        if future is not None and not future.done():
            future.set_result(code)
            return web.Response(
                text="Authorization successful! You can close this window and return to Home Assistant.",
                content_type="text/html",
            )

        _LOGGER.warning("OAuth callback received (flow_id=%s) but no pending future was found", flow_id)
        return web.Response(
            text="Authorization request not found or already completed. Please return to Home Assistant and try again.",
            status=400,
        )
