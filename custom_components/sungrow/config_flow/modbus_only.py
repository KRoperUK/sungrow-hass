"""Modbus-only (cloud-free) transport steps (#354).

Covers direct-Modbus setup without any iSolarCloud credentials:

- ``async_step_local_setup`` — manual host + serial + model form
- ``async_step_import`` — programmatic entry creation (legacy hybrid split)
- ``async_step_reconfigure_modbus`` — update the WiNet-S host on an existing entry

Zeroconf discovery for the same transport lives in :mod:`.zeroconf`.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult

from ..const import (
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    TRANSPORT_MODBUS_ONLY,
)
from ._base import _SungrowFlowBase

_LOGGER = logging.getLogger(__name__)


class ModbusOnlyMixin(_SungrowFlowBase):
    """Modbus-only setup / import / reconfigure steps for :class:`SungrowConfigFlow`."""

    async def async_step_local_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect host, serial, and model for a fully local Modbus Only entry (#216)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
            serial = (user_input.get(CONF_SERIAL) or "").strip()
            model = (user_input.get(CONF_MODEL) or "Inverter").strip()

            from ..helpers import async_test_modbus_host

            if await async_test_modbus_host(host):
                await self.async_set_unique_id(f"modbus_{serial}")
                self._abort_if_unique_id_configured(updates={CONF_MODBUS_HOST: host})
                return self.async_create_entry(
                    title=f"Sungrow {model} (local)",
                    data={
                        CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                        CONF_SERIAL: serial,
                        CONF_MODEL: model,
                        CONF_MODBUS_HOST: host,
                    },
                    options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
                )
            errors["base"] = "host_unreachable"

        return self.async_show_form(
            step_id="local_setup",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST): str,
                    vol.Required(CONF_SERIAL): str,
                    vol.Required(CONF_MODEL, default="Inverter"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Import a Modbus-only entry (legacy hybrid split / programmatic setup)."""
        serial = str(user_input.get(CONF_SERIAL) or "").strip()
        host = str(user_input.get(CONF_MODBUS_HOST) or "").strip()
        if not serial or not host:
            return self.async_abort(reason="not_sungrow_device")
        model = str(user_input.get(CONF_MODEL) or "Inverter")
        await self.async_set_unique_id(f"modbus_{serial}")
        self._abort_if_unique_id_configured(updates={CONF_MODBUS_HOST: host})
        options: dict[str, Any] = {
            CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL)),
        }
        if user_input.get(CONF_MODBUS_DEBUG_DAILY_YIELD):
            options[CONF_MODBUS_DEBUG_DAILY_YIELD] = True
        return self.async_create_entry(
            title=f"Sungrow {model} (local)",
            data={
                CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                CONF_SERIAL: serial,
                CONF_MODEL: model,
                CONF_MODBUS_HOST: host,
            },
            options=options,
        )

    async def async_step_reconfigure_modbus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure a cloud-free Modbus-only entry: update the WiNet-S host (#159).

        No credentials are involved — the only thing worth changing is the local IP, in
        case the WiNet-S moved to a new DHCP lease and discovery did not re-announce.
        """
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # Blank means "leave unchanged" so reconfigure can never accidentally clear
            # the host (which would take the entry offline).
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip() or entry.data.get(CONF_MODBUS_HOST)
            return self.async_update_reload_and_abort(
                entry,
                data={**entry.data, CONF_MODBUS_HOST: host},
                reason="reconfigure_successful",
            )
        return self.async_show_form(
            step_id="reconfigure_modbus",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=entry.data.get(CONF_MODBUS_HOST, "")): str,
                }
            ),
        )
