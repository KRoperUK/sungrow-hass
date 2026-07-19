"""Options flow for the Sungrow integration (#354).

Subclasses ``OptionsFlowWithReload`` so an options change reloads the entry
automatically. This replaces the old manual ``add_update_listener`` — which
also fired on every token rotation (a plain ``entry.data`` write) and reloaded
the whole integration on each refresh (#110). Because
``OptionsFlowWithReload`` forbids config-entry update listeners, none are
registered in ``__init__``.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from ..const import (
    CONF_ENABLE_DEVICE_SENSORS,
    CONF_EXTRA_MEASURE_POINTS,
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_SCAN_INTERVAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    TRANSPORT_MODBUS_ONLY,
)
from ._helpers import _parse_extra_measure_points

_LOGGER = logging.getLogger(__name__)


class SungrowOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle Sungrow integration options (e.g. polling interval).

    Subclasses ``OptionsFlowWithReload`` so an options change reloads the entry
    automatically. This replaces the old manual ``add_update_listener`` — which
    also fired on every token rotation (a plain ``entry.data`` write) and reloaded
    the whole integration on each refresh (#110). Because ``OptionsFlowWithReload``
    forbids config-entry update listeners, none are registered in ``__init__``.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the integration options."""
        transport = self.config_entry.data.get(CONF_TRANSPORT)

        # A cloud-free Modbus-only entry has none of the cloud settings (API quota,
        # extra measure points, per-device fetch); show only the local poll interval (#159).
        if transport == TRANSPORT_MODBUS_ONLY:
            return await self.async_step_modbus_options(user_input)

        errors: dict[str, str] = {}
        if user_input is not None:
            # Normalise the free-text mapping into a dict before storing.
            try:
                extras = _parse_extra_measure_points(user_input.get(CONF_EXTRA_MEASURE_POINTS))
            except vol.Invalid as exc:
                errors["base"] = "invalid_extra_measure_points"
                _LOGGER.warning("Invalid extra measure points input: %s", exc)
            else:
                data = {**user_input, CONF_EXTRA_MEASURE_POINTS: extras}
                data.pop(CONF_MODBUS_HOST, None)
                data.pop(CONF_MODBUS_DEBUG_DAILY_YIELD, None)
                # cloud_modbus transport was retired in #348; the options flow no
                # longer offers modbus_host on cloud entries. Local Modbus is set up
                # via a separate ``Modbus Only`` entry instead.
                if not errors:
                    return self.async_create_entry(title="", data=data)

        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        current_extras = self.config_entry.options.get(CONF_EXTRA_MEASURE_POINTS, {})
        current_device_sensors = self.config_entry.options.get(CONF_ENABLE_DEVICE_SENSORS, False)
        extras_str = ",".join(f"{pid}={code}" for pid, code in current_extras.items())

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            ),
            vol.Optional(
                CONF_EXTRA_MEASURE_POINTS,
                default=extras_str,
                description={"suggested_value": extras_str},
            ): str,
            vol.Optional(CONF_ENABLE_DEVICE_SENSORS, default=current_device_sensors): bool,
        }

        # Local Modbus is a separate entry now (#348 retired the ``cloud_modbus``
        # transport). The options flow for a cloud entry no longer offers a
        # ``modbus_host`` field — users who want local Modbus add a ``Modbus Only``
        # entry alongside their cloud one.

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    async def async_step_modbus_options(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Options for a cloud-free Modbus-only entry: just the local poll interval (#159).

        The cloud settings (API quota, extra measure points, per-device fetch, the
        optional-Modbus-host toggle) are all meaningless here, so none are shown. The
        WiNet-S host is managed by discovery, not the options flow.
        """
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                    CONF_MODBUS_DEBUG_DAILY_YIELD: bool(user_input.get(CONF_MODBUS_DEBUG_DAILY_YIELD, False)),
                },
            )
        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_MODBUS_SCAN_INTERVAL)
        current_debug_daily = bool(self.config_entry.options.get(CONF_MODBUS_DEBUG_DAILY_YIELD, False))
        return self.async_show_form(
            step_id="modbus_options",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                    vol.Optional(CONF_MODBUS_DEBUG_DAILY_YIELD, default=current_debug_daily): bool,
                }
            ),
        )
