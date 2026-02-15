"""The Sungrow iSolarCloud integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# TODO List the platforms that you want to support.
# For your initial example we don't have sensors yet but usually:
# PLATFORMS: list[Platform] = [Platform.SENSOR]
PLATFORMS: list[Platform] = [Platform.SENSOR]


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

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Sungrow iSolarCloud component."""
    hass.http.register_view(SungrowAuthCallbackView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Sungrow iSolarCloud from a config entry."""

    # TODO: Initialize the API client here using entry.data
    # For now, just store the config so we know it's loaded
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


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
