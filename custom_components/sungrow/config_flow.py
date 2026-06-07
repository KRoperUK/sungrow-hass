"""Config flow for Sungrow iSolarCloud integration."""

import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

from .const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_REDIRECT_URI,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GATEWAYS,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

# Try to import pysolarcloud, handle if missing gracefully for development
try:
    from pysolarcloud import Auth
except ImportError:
    Auth = None
    # For local development if pysolarcloud is not installed but in path or similar
    # In a real environment, it should be installed via requirements.

_LOGGER = logging.getLogger(__name__)


class SungrowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow iSolarCloud."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self.init_info = {}
        self.auth_client = None
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SungrowOptionsFlow":
        """Return the options flow handler."""
        return SungrowOptionsFlow()

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Handle re-authentication when the stored tokens are no longer valid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        # Reuse the credentials already stored on the entry; only the tokens are stale.
        self.init_info = {k: v for k, v in entry_data.items() if k != "tokens"}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm re-authentication by re-running the authorization step."""
        return await self.async_step_auth(user_input)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        _LOGGER.debug(f"async_step_user called with user_input: {user_input}")
        errors = {}

        if user_input is not None:
            self.init_info = user_input
            return await self.async_step_auth()

        # Attempt to automatically detect the callback URL
        try:
            base_url = get_url(self.hass, allow_internal=False, allow_external=True)
        except Exception:
            base_url = "http://homeassistant.local:8123"  # Fallback

        default_redirect = f"{base_url}/api/sungrow_hass/callback"

        self.context["title_placeholders"] = {
            "app_id": user_input.get(CONF_APP_ID, "YourAppID") if user_input else "YourAppID"
        }

        description_placeholders = {
            "url": "https://developer-api.isolarcloud.com/#/application",
            "app_id_url": "https://developer-api.isolarcloud.com/#/editApplication?id=1234",
        }

        return self.async_show_form(
            step_id="user",
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

    async def async_step_auth(self, user_input: dict[str, Any] | None = None):
        """Handle the authorization step."""
        errors = {}

        # Initialize Auth client
        if not self.auth_client:
            session = async_get_clientsession(self.hass)
            gateway_url = GATEWAYS[self.init_info[CONF_GATEWAY]]

            # Ensure Auth is available
            if Auth is None:
                # Fallback or error if library missing
                return self.async_abort(reason="library_missing")

            self.auth_client = Auth(
                host=gateway_url,
                appkey=self.init_info[CONF_APP_KEY],
                access_key=self.init_info[CONF_APP_SECRET],
                app_id=self.init_info[CONF_APP_ID],
                websession=session,
            )
            _LOGGER.info("Initialized Auth client for Sungrow iSolarCloud")
            _LOGGER.debug(f"Auth client details: {self.auth_client}")

        if user_input is not None and user_input.get("code"):
            try:
                code_input = user_input["code"].strip()
                if code_input.startswith("http"):
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(code_input)
                    query = parse_qs(parsed.query)
                    if "code" in query:
                        code = query["code"][0]
                    else:
                        # Fallback if maybe it's in the fragment?
                        query = parse_qs(parsed.fragment.split("?")[-1] if "?" in parsed.fragment else "")
                        if "code" in query:
                            code = query["code"][0]
                        else:
                            errors["base"] = "invalid_auth"
                            raise ValueError("Could not find code in URL")
                else:
                    code = code_input

                # We need the base redirect URI without the flow_id param for the token exchange
                # The code we get implies the user was redirected to: URI?flow_id=123&code=ABC
                # For exchange, we must send the exact SAME redirect_uri sent in auth request.

                # Use the clean redirect URI without flow_id for the token exchange
                # This ensures it matches exactly what was sent in the auth request
                # (and prevents issues if the provider strips query params)
                redirect_uri_clean = self.init_info[CONF_REDIRECT_URI]

                _LOGGER.info("Authorizing with code: %s and redirect_uri: %s", code, redirect_uri_clean)
                await self.auth_client.async_authorize(code, redirect_uri_clean)

                # Get the tokens
                tokens = self.auth_client.tokens
                _LOGGER.debug(f"Received tokens: {tokens}")

                if not tokens or not tokens.get("access_token"):
                    _LOGGER.error("Failed to retrieve tokens")
                    errors["base"] = "invalid_auth"
                else:
                    # We can store tokens in the config entry data
                    data = {**self.init_info, "tokens": tokens}

                    if self._reauth_entry is not None:
                        # Update the existing entry in place and reload it, so the user
                        # never has to delete and re-add the integration (issue #14).
                        return self.async_update_reload_and_abort(self._reauth_entry, data=data)

                    await self.async_set_unique_id(str(self.init_info[CONF_APP_ID]))
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=f"Sungrow {self.init_info[CONF_APP_ID]}", data=data)

            except data_entry_flow.AbortFlow:
                # Let HA handle flow aborts (e.g. already_configured) normally.
                raise
            except ClientError as e:
                _LOGGER.warning("Client connection error in async_step_auth: %s", e)
                errors["base"] = "cannot_connect"
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception in async_step_auth: %s", e)
                errors["base"] = "unknown"

        # Generate auth URL
        redirect_uri_clean = self.init_info[CONF_REDIRECT_URI]

        # We use the clean URI (no flow_id) to potentially avoid provider issues with query params
        auth_url = self.auth_client.auth_url(redirect_uri_clean)

        return self.async_show_form(
            step_id="auth",
            description_placeholders={"auth_url": auth_url},
            data_schema=vol.Schema({vol.Optional("code"): str}),
            errors=errors,
        )


class SungrowOptionsFlow(config_entries.OptionsFlow):
    """Handle Sungrow integration options (e.g. polling interval)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage the integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    )
                }
            ),
        )
