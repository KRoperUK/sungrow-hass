"""Reauth + reconfigure steps for the Sungrow config flow (#354).

These steps rewire an existing config entry rather than creating a new one:

- ``async_step_reauth`` — HA calls this when stored tokens/credentials expire.
  Dispatches to the correct per-transport step (user-account reauth reuses
  ``async_step_cloud_user``; OAuth reauth funnels back through ``async_step_auth``).
- ``async_step_reauth_confirm`` — accept the reauth trigger and start
  authorization.
- ``async_step_reconfigure`` — cloud-entry re-authorization with editable
  gateway / keys / redirect URI (Modbus-only reconfigure lives in
  :mod:`.modbus_only` since it changes different fields).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult

from ..const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_REDIRECT_URI,
    CONF_TRANSPORT,
    GATEWAYS,
    TRANSPORT_CLOUD_USER,
    TRANSPORT_MODBUS_ONLY,
)
from ._base import _SungrowFlowBase
from ._helpers import _normalize_redirect_uri

_LOGGER = logging.getLogger(__name__)


class ReconfigureAndReauthMixin(_SungrowFlowBase):
    """Reauth + reconfigure steps for :class:`SungrowConfigFlow`."""

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when the stored tokens/credentials are no longer valid."""
        self._reauth_entry = self._get_reauth_entry()
        # A user-account entry has no OAuth tokens — reauth just re-collects the password
        # (the account/region are reused as defaults) (#268).
        if entry_data.get(CONF_TRANSPORT) == TRANSPORT_CLOUD_USER:
            return await self.async_step_cloud_user()  # type: ignore[attr-defined,no-any-return]
        # Reuse the credentials already stored on the entry; only the tokens are stale.
        self.init_info = {k: v for k, v in entry_data.items() if k != "tokens"}
        # If the entry is missing the App ID (legacy/corrupted), we cannot proceed
        # with auth — the user must reconfigure to supply the missing credential (#245).
        if not self.init_info.get(CONF_APP_ID):
            return self.async_abort(reason="missing_app_id")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm re-authentication by re-running the authorization step."""
        return await self.async_step_auth(user_input)  # type: ignore[attr-defined,no-any-return]

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the gateway / credentials of an existing entry, then re-authorize.

        The App ID is the entry's identity (unique_id) so it stays fixed; everything
        else — region, keys, redirect URI — is editable. Because new credentials or a
        new region invalidate the stored tokens, submitting always re-runs the OAuth
        authorization and updates the entry in place (no delete & re-add).

        For entries migrated from older versions that are missing the App ID, the
        reconfigure form requests it so the user can supply the value without having
        to delete and recreate the entry (#245).
        """
        entry = self._get_reconfigure_entry()
        transport = entry.data.get(CONF_TRANSPORT)
        # A cloud-free Modbus-only entry has no credentials to change; reconfigure just
        # updates the WiNet-S host (#159), not the cloud app key/secret/gateway.
        if transport == TRANSPORT_MODBUS_ONLY:
            return await self.async_step_reconfigure_modbus(user_input)  # type: ignore[attr-defined,no-any-return]
        self._reauth_entry = entry
        self._is_reconfigure = True
        self._transport = transport

        errors: dict[str, str] = {}
        if user_input is not None:
            fixed_uri = _normalize_redirect_uri(user_input.get(CONF_REDIRECT_URI))
            if fixed_uri is None:
                errors[CONF_REDIRECT_URI] = "invalid_redirect_uri"
            else:
                user_input = {**user_input, CONF_REDIRECT_URI: fixed_uri}
                # Preserve the App ID (identity) and drop stale tokens — we re-authorize.
                self.init_info = {**entry.data, **user_input}
                self.init_info.pop("tokens", None)
                # Start a fresh Auth client for the (possibly changed) credentials.
                self.auth_client = None
                return await self.async_step_auth()  # type: ignore[attr-defined,no-any-return]

        current = entry.data
        # Build the schema dynamically: include App ID when the entry is missing it
        # (legacy entries upgraded from older versions may lack this field).
        schema_fields: dict[Any, Any] = {}
        if not current.get(CONF_APP_ID):
            schema_fields[vol.Required(CONF_APP_ID, default="")] = str
        schema_fields[vol.Required(CONF_APP_KEY, default=current.get(CONF_APP_KEY, ""))] = str
        schema_fields[vol.Required(CONF_APP_SECRET, default=current.get(CONF_APP_SECRET, ""))] = str
        schema_fields[vol.Required(CONF_GATEWAY, default=current.get(CONF_GATEWAY, "Europe"))] = vol.In(
            list(GATEWAYS.keys())
        )
        schema_fields[vol.Required(CONF_REDIRECT_URI, default=current.get(CONF_REDIRECT_URI, ""))] = str

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )
