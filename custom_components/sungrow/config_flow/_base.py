"""Instance state + lifecycle base for the Sungrow config flow (#354).

Every per-transport mixin inherits from :class:`_SungrowFlowBase` so ``self``
carries the same attributes (``init_info``, ``auth_client``, ``_state``, …) no
matter which transport is driving. The shell class in :mod:`config_flow` then
inherits every mixin (which each inherit this base) and ``ConfigFlow``'s
`__init_subclass__` registers it with HA via ``domain=DOMAIN``.

Mixin classes deliberately do NOT pass ``domain=`` when subclassing this base,
so only the concrete shell class registers itself as a config flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import DOMAIN

# Try to import pysolarcloud, handle if missing gracefully for development.
try:
    from pysolarcloud import Auth, AuthError, PySolarCloudException, UserAuth
except ImportError:
    # Optional import for local dev; production always has it via requirements. The
    # ``unused-ignore`` keeps this compatible with both pre-0.15.0 library type shapes
    # (where the fallback assignments needed the ignore) and 0.15.0+ (where the strict
    # types make it unnecessary).
    Auth = None  # type: ignore[assignment,misc,unused-ignore]
    UserAuth = None  # type: ignore[assignment,misc,unused-ignore]
    AuthError = PySolarCloudException = Exception  # type: ignore[assignment,misc,unused-ignore]

_LOGGER = logging.getLogger(__name__)

# Every mixin dot-looks up these library symbols via ``_base.<name>`` rather than
# from-importing them into its own namespace. That gives the tests a *single*
# patch target (``config_flow._base.Auth`` etc.) instead of one per submodule —
# the original monolithic ``config_flow.py`` had just one binding, so preserving
# that single-target patch shape keeps the test surface simple.
__all__ = [
    "Auth",
    "AuthError",
    "PySolarCloudException",
    "UserAuth",
    "_SungrowFlowBase",
    "async_get_clientsession",
]


class _SungrowFlowBase(config_entries.ConfigFlow):
    """Shared state + lifecycle for every per-transport step mixin.

    Not registered as a config flow itself — no ``domain=`` kwarg, so
    ``ConfigFlow.__init_subclass__`` skips registration. Only the concrete
    :class:`~custom_components.sungrow.config_flow.SungrowConfigFlow` shell in
    :mod:`config_flow` passes ``domain=DOMAIN`` and enters HA's flow manager.
    """

    VERSION = 5

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.init_info: dict[str, Any] = {}
        self.auth_client: Any = None
        self._reauth_entry: ConfigEntry | None = None
        self._is_reconfigure = False
        # OAuth flow state (kept on the instance, which persists across steps and the
        # callback background task, rather than in the typed ConfigFlowContext).
        self._code: str | None = None
        self._callback_timeout = False
        # OAuth ``state`` token that correlates the callback redirect back to this
        # exact flow (registered in hass.data so the callback view can look it up).
        self._state: str | None = None
        # Zeroconf-discovered WiNet-S local Modbus host, carried into the confirm step
        # that creates the cloud-free Modbus-only entry (#159).
        self._discovered_modbus_host: str | None = None
        # Transport mode selected in the transport step (#216).
        self._transport: str | None = None

    @callback
    def async_remove(self) -> None:
        """Prune this flow's pending OAuth future and state when the flow is removed.

        HA calls this on flow completion or abort. Dropping the future (cancelling it
        if still pending) and its ``state`` mapping stops stale correlators from
        lingering for up to ``CALLBACK_WAIT_TIMEOUT`` — which is what let a later,
        legitimate redirect land on an abandoned flow or trip ``len(flows) != 1`` and
        400 a valid setup (#116).
        """
        domain_data = self.hass.data.get(DOMAIN, {})
        flows = domain_data.get("flows")
        if isinstance(flows, dict):
            future = flows.pop(self.flow_id, None)
            if isinstance(future, asyncio.Future) and not future.done():
                future.cancel()
        states = domain_data.get("states")
        if isinstance(states, dict) and self._state is not None:
            states.pop(self._state, None)
