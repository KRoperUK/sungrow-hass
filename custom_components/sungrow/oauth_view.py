"""OAuth callback HTTP view for the Sungrow integration.

Handles the iSolarCloud redirect back to Home Assistant after the user authorizes
the developer application. Extracted from ``__init__.py`` (#289).
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


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
            _LOGGER.warning("Callback received but missing 'code'. Query params present: %s", list(params))
            return web.Response(text="Missing code parameter. Please try again.", status=400)

        _LOGGER.debug("Callback received with an authorization code (flow_id=%s, state=%s)", flow_id, state)

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
