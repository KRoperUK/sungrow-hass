"""The Sungrow iSolarCloud integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pysolarcloud.plants import Plants

from .auth import SungrowAuth
from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    DEFAULT_HOST,
    DOMAIN,
    GATEWAYS,
)
from .coordinator import SungrowPlantCoordinator, is_auth_error

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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

    try:
        plant_list = await plants_service.async_get_plants()
    except Exception as err:
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(f"Authentication with iSolarCloud failed: {err}") from err
        raise ConfigEntryNotReady(f"Unable to fetch plants from iSolarCloud: {err}") from err

    coordinators: list[SungrowPlantCoordinator] = []
    for plant_info in plant_list:
        plant_id = str(plant_info["ps_id"])
        plant_name = plant_info["ps_name"]
        coordinator = SungrowPlantCoordinator(hass, entry, plants_service, plant_id, plant_name)
        # Raises ConfigEntryNotReady / ConfigEntryAuthFailed as classified by the coordinator.
        await coordinator.async_config_entry_first_refresh()
        coordinators.append(coordinator)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when its options (e.g. scan interval) change.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


class SungrowAuthCallbackView(HomeAssistantView):
    """Sungrow Authorization Callback View."""

    requires_auth = False
    url = "/api/sungrow_hass/callback"
    name = "api:sungrow_hass:callback"

    async def get(self, request: web.Request) -> web.Response:
        """Handle callback."""
        hass: HomeAssistant = request.app["hass"]
        params = request.query
        code = params.get("code")
        flow_id = params.get("flow_id")

        if not code or not flow_id:
            _LOGGER.warning("Callback received but missing code or flow_id. Params: %s", params)
            return web.Response(text="Missing code or flow_id parameters. Please try again.", status=400)

        _LOGGER.debug("Callback received with code: %s for flow_id: %s", code, flow_id)

        # Retrieve the flow and update it
        try:
            await hass.config_entries.flow.async_configure(flow_id=flow_id, user_input={"code": code})
        except Exception as err:
            _LOGGER.error("Failed to pass code to config flow: %s", err)
            return web.Response(text=f"Error occurred while resuming flow: {err}", status=500)

        return web.Response(
            text="Authorization successful! You can close this window and return to Home Assistant.",
            content_type="text/html",
        )
