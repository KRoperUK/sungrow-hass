"""Config flow for the Sungrow iSolarCloud integration.

hassfest requires the ConfigFlow subclass to be textually defined in a file
named ``config_flow.py``. The per-transport modules — cloud OAuth,
cloud user-account, Modbus-only, zeroconf, reauth/reconfigure, options,
plus helpers — live in the sibling :mod:`._config_flow` subpackage so
editing one transport doesn't risk touching another and the mental map
is smaller per file (#354).

Layout:

* :mod:`._config_flow._base` — shared instance state + lifecycle (``async_remove``).
* :mod:`._config_flow._helpers` — pure helper functions (URI normalisation,
  TXT parsing, extra-measure-point parsing) and the OAuth callback timeout
  constant.
* :mod:`._config_flow.cloud_oauth` — developer-portal OAuth handshake steps
  + helpers.
* :mod:`._config_flow.cloud_user` — unofficial email/password (``UserAuth``)
  transport.
* :mod:`._config_flow.modbus_only` — cloud-free direct-Modbus setup / import
  / reconfigure.
* :mod:`._config_flow.zeroconf` — WiNet-S mDNS discovery.
* :mod:`._config_flow.reconfigure` — reauth + cloud reconfigure.
* :mod:`._config_flow.options` — options-flow handler.

The shell class :class:`SungrowConfigFlow` below combines every per-transport
mixin (each of which subclasses ``_SungrowFlowBase``) and passes
``domain=DOMAIN`` so it's the only class HA registers as a config flow.
``async_step_user`` — the transport selector that dispatches into each
per-transport step — lives here since it orchestrates all of them, as does
``async_step_cloud_credentials`` (the cloud fork of the user step).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.network import get_url

from ._config_flow._helpers import _normalize_redirect_uri, _parse_winet_properties
from ._config_flow.cloud_oauth import CloudOAuthMixin
from ._config_flow.cloud_user import CloudUserMixin
from ._config_flow.modbus_only import ModbusOnlyMixin
from ._config_flow.options import SungrowOptionsFlow
from ._config_flow.reconfigure import ReconfigureAndReauthMixin
from ._config_flow.zeroconf import ZeroconfMixin
from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_REDIRECT_URI,
    CONF_TRANSPORT,
    DOMAIN,
    GATEWAYS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_CLOUD_USER,
    TRANSPORT_MODBUS_ONLY,
)
from .oauth_view import OAUTH_CALLBACK_PATH

_LOGGER = logging.getLogger(__name__)

# Re-exports for external consumers (tests import these paths directly).
__all__ = [
    "SungrowConfigFlow",
    "SungrowOptionsFlow",
    "_normalize_redirect_uri",
    "_parse_winet_properties",
]


class SungrowConfigFlow(
    ReconfigureAndReauthMixin,
    ZeroconfMixin,
    ModbusOnlyMixin,
    CloudOAuthMixin,
    CloudUserMixin,
    domain=DOMAIN,
):
    """Config flow for Sungrow iSolarCloud, assembled from per-transport mixins.

    Each mixin subclasses :class:`._config_flow._base._SungrowFlowBase` (which
    is a bare ``ConfigFlow`` subclass without ``domain=``), so ``self`` in every
    step method sees the shared instance state and helper methods. The concrete
    shell class here is the only subclass that registers with HA's flow manager
    (via ``domain=DOMAIN`` above).
    """

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SungrowOptionsFlow:
        """Return the options flow handler."""
        return SungrowOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Present the transport-mode selector as the first step (#216)."""
        if user_input is not None:
            transport = user_input[CONF_TRANSPORT]
            self._transport = transport
            if transport == TRANSPORT_MODBUS_ONLY:
                return await self.async_step_local_setup()
            if transport == TRANSPORT_CLOUD_USER:
                return await self.async_step_cloud_user()
            # cloud_only / (retired cloud_modbus) → cloud credentials
            return await self.async_step_cloud_credentials()

        from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig

        transport_options = [
            SelectOptionDict(
                value=TRANSPORT_CLOUD_ONLY,
                label="Cloud (Developer Account via Official OpenAPI - Cloud Polling)",
            ),
            SelectOptionDict(
                value=TRANSPORT_CLOUD_USER,
                label="Cloud (User Account via Unofficial API - Cloud Polling)",
            ),
            SelectOptionDict(value=TRANSPORT_MODBUS_ONLY, label="Modbus (Local Polling)"),
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSPORT, default=TRANSPORT_CLOUD_ONLY): SelectSelector(
                        SelectSelectorConfig(options=transport_options, translation_key="transport")
                    ),
                }
            ),
        )

    async def async_step_cloud_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect iSolarCloud API credentials (formerly ``async_step_user``).

        Lives on the shell class because it doesn't belong to any single transport
        mixin — it's the "cloud path" fork of the user step, feeding either the
        OAuth flow (``async_step_auth``) or the retired cloud+modbus code path.
        """
        _LOGGER.debug("async_step_cloud_credentials called (user_input provided: %s)", user_input is not None)
        errors: dict[str, str] = {}

        if user_input is not None:
            # Normalise / validate the redirect URI: iSolarCloud drops the auth code if
            # it redirects anywhere other than the OAuth callback view (#340). Auto-fix
            # obvious bare-host inputs; otherwise reject with a clear error so the user
            # updates BOTH this field and the developer-portal registration.
            fixed_uri = _normalize_redirect_uri(user_input.get(CONF_REDIRECT_URI))
            if fixed_uri is None:
                errors[CONF_REDIRECT_URI] = "invalid_redirect_uri"
            else:
                user_input = {**user_input, CONF_REDIRECT_URI: fixed_uri}
                self.init_info = user_input
                await self.async_set_unique_id(str(user_input[CONF_APP_ID]))
                self._abort_if_unique_id_configured()

                # Create the hub immediately, before authorizing. Setting up this
                # token-less entry runs async_setup — which registers the OAuth
                # callback view — and then raises ConfigEntryAuthFailed, so Home
                # Assistant starts a reauth flow to finish authorization.
                data = {**user_input, CONF_TRANSPORT: self._transport or TRANSPORT_CLOUD_ONLY}
                return self.async_create_entry(
                    title=f"Sungrow {user_input[CONF_APP_ID]}",
                    data=data,
                )

        # Attempt to automatically detect the callback URL
        try:
            base_url = get_url(self.hass, allow_internal=False, allow_external=True)
        except Exception:  # pylint: disable=broad-except
            base_url = "http://homeassistant.local:8123"  # Fallback

        default_redirect = f"{base_url}{OAUTH_CALLBACK_PATH}"

        self.context["title_placeholders"] = {"app_id": "YourAppID"}

        description_placeholders = {
            "url": "https://developer-api.isolarcloud.com/#/application",
            "app_id_url": "https://developer-api.isolarcloud.com/#/editApplication?id=1234",
        }

        return self.async_show_form(
            step_id="cloud_credentials",
            description_placeholders=description_placeholders,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_KEY): str,
                    vol.Required(CONF_APP_SECRET): str,
                    vol.Required(CONF_APP_ID, default=""): str,
                    vol.Required(CONF_GATEWAY, default="Europe"): vol.In(list(GATEWAYS.keys())),
                    vol.Required(CONF_REDIRECT_URI, default=default_redirect): str,
                }
            ),
            errors=errors,
        )
