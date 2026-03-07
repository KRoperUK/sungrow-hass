"""Config flow for Sungrow iSolarCloud integration (API v1 - username/password)."""

import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    GATEWAYS,
)
from .isolarcloud_api import ISolarCloudAPI, ISolarCloudError

_LOGGER = logging.getLogger(__name__)


class SungrowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow iSolarCloud."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step - collect credentials and login."""
        errors = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            gateway_url = GATEWAYS[user_input[CONF_GATEWAY]]

            api = ISolarCloudAPI(
                host=gateway_url,
                appkey=user_input[CONF_APP_KEY],
                access_key=user_input[CONF_APP_SECRET],
                user_account=user_input[CONF_USERNAME],
                user_password=user_input[CONF_PASSWORD],
                websession=session,
            )

            try:
                login_data = await api.async_login()

                data = {
                    **user_input,
                    "token": api.token,
                    "user_id": api.user_id,
                }

                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Sungrow {user_input[CONF_USERNAME]}",
                    data=data,
                )

            except ISolarCloudError as err:
                _LOGGER.warning("iSolarCloud API error: %s", err)
                errors["base"] = "invalid_auth"
            except ClientError as err:
                _LOGGER.warning("Connection error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error: %s", err)
                errors["base"] = "unknown"

        description_placeholders = {
            "url": "https://developer-api.isolarcloud.com/#/application",
        }

        return self.async_show_form(
            step_id="user",
            description_placeholders=description_placeholders,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APP_KEY): str,
                    vol.Required(CONF_APP_SECRET): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_GATEWAY, default="Europe"): vol.In(list(GATEWAYS.keys())),
                }
            ),
            errors=errors,
        )
