"""Cloud user-account (email/password) transport step (#354).

Wraps the unofficial iSolarCloud app/web API (:class:`pysolarcloud.UserAuth`).
Used for both initial setup and reauth — the reauth path re-enters this same
step with ``self._reauth_entry`` set so the credentials are updated on the
existing entry rather than creating a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import ConfigFlowResult

from ..const import (
    CONF_GATEWAY,
    CONF_TRANSPORT,
    CONF_USER_ACCOUNT,
    CONF_USER_PASSWORD,
    GATEWAYS,
    TRANSPORT_CLOUD_USER,
)
from . import _base
from ._base import _SungrowFlowBase
from .plant_selection import PlantSelectionMixin

_LOGGER = logging.getLogger(__name__)


class CloudUserMixin(PlantSelectionMixin, _SungrowFlowBase):
    """User-account cloud transport step for :class:`SungrowConfigFlow`."""

    async def async_step_cloud_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect iSolarCloud user-account credentials for the unofficial cloud_user transport (#268).

        Used for both initial setup and reauth (when ``self._reauth_entry`` is set). Validates
        the credentials by logging in and listing plants via ``UserAuth`` before creating/updating
        the entry. The password is stored in the config entry and never logged.
        """
        from homeassistant.helpers.selector import (
            SelectOptionDict,
            SelectSelector,
            SelectSelectorConfig,
            TextSelector,
            TextSelectorConfig,
            TextSelectorType,
        )

        reauth_entry = self._reauth_entry
        errors: dict[str, str] = {}

        if user_input is not None:
            email = (user_input.get(CONF_USER_ACCOUNT) or "").strip()
            password = user_input.get(CONF_USER_PASSWORD) or ""
            gateway = user_input.get(CONF_GATEWAY) or "Europe"
            plant_list: list[dict[str, Any]] = []
            if _base.UserAuth is None:
                errors["base"] = "unknown"
            else:
                session = _base.async_get_clientsession(self.hass)
                client = _base.UserAuth(GATEWAYS[gateway], email, password, websession=session)
                try:
                    async with asyncio.timeout(30):
                        plant_list = list(await client.async_get_plants() or [])
                except _base.AuthError:
                    errors["base"] = "invalid_auth"
                except (_base.PySolarCloudException, ClientError, TimeoutError):
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected error validating user-account login")
                    errors["base"] = "unknown"
            if not errors:
                data = {
                    CONF_TRANSPORT: TRANSPORT_CLOUD_USER,
                    CONF_USER_ACCOUNT: email,
                    CONF_USER_PASSWORD: password,
                    CONF_GATEWAY: gateway,
                }
                # Multi-plant accounts route through the picker (#358); single-plant
                # accounts skip it so the flow shape stays identical to pre-#358 for
                # the common case.
                if len(plant_list) > 1:
                    self._pending_plant_list = plant_list
                    self._pending_entry_data = data
                    return await self.async_step_plant_selection()
                return await self._finalise_cloud_user_entry(data)

        default_email = reauth_entry.data.get(CONF_USER_ACCOUNT, "") if reauth_entry is not None else ""
        default_gateway = reauth_entry.data.get(CONF_GATEWAY, "Europe") if reauth_entry is not None else "Europe"
        region_options = [SelectOptionDict(value=name, label=name) for name in GATEWAYS]
        schema = vol.Schema(
            {
                vol.Required(CONF_USER_ACCOUNT, default=default_email): str,
                vol.Required(CONF_USER_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(CONF_GATEWAY, default=default_gateway): SelectSelector(
                    SelectSelectorConfig(options=region_options)
                ),
            }
        )
        return self.async_show_form(step_id="cloud_user", data_schema=schema, errors=errors)

    async def _finalise_cloud_user_entry(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Create / update the cloud_user entry with the plant selection merged in.

        Called by :class:`PlantSelectionMixin._dispatch_plant_selection_finalise`
        after the user submits the picker, and by :meth:`async_step_cloud_user`
        directly for single-plant accounts that skip the picker entirely.
        """
        reauth_entry = self._reauth_entry
        if reauth_entry is not None:
            return self.async_update_reload_and_abort(reauth_entry, data=entry_data)
        email = str(entry_data.get(CONF_USER_ACCOUNT) or "")
        await self.async_set_unique_id(f"user_{email.lower()}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Sungrow ({email})", data=entry_data)
