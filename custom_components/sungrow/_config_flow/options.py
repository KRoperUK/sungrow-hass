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
from collections.abc import Mapping
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
    CONF_SCHEDULE_WINDOWS,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    TRANSPORT_MODBUS_ONLY,
)
from ._helpers import _parse_extra_measure_points

# Fixed number of schedule slots exposed in the options-flow UI (#359). Two covers
# the common tariff pattern (cheap overnight charge + optional peak discharge); users
# who need more can add windows via YAML options edits or wait for a follow-up UI.
_SCHEDULE_SLOTS = 2

# Mode options offered per schedule slot, mirroring the keys accepted by
# ``sungrow.set_battery_mode`` and :mod:`..schedule`.
_SCHEDULE_MODE_OPTIONS = ("force_charge", "force_discharge")

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
                schedule_windows, schedule_errors = _collect_schedule_windows(user_input)
                errors.update(schedule_errors)
                if not errors:
                    data = {**user_input, CONF_EXTRA_MEASURE_POINTS: extras}
                    data.pop(CONF_MODBUS_HOST, None)
                    data.pop(CONF_MODBUS_DEBUG_DAILY_YIELD, None)
                    # Strip the per-slot schedule fields — they're stored as a single
                    # normalised list under ``CONF_SCHEDULE_WINDOWS`` (#359).
                    for slot in range(1, _SCHEDULE_SLOTS + 1):
                        data.pop(f"schedule_{slot}_start", None)
                        data.pop(f"schedule_{slot}_end", None)
                        data.pop(f"schedule_{slot}_mode", None)
                    data[CONF_SCHEDULE_WINDOWS] = schedule_windows
                    # cloud_modbus transport was retired in #348; the options flow no
                    # longer offers modbus_host on cloud entries. Local Modbus is set up
                    # via a separate ``Modbus Only`` entry instead.
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

        # Scheduled forced-charge / forced-discharge windows (#359). Two fixed slots
        # cover the typical tariff shape; each slot is a triple of start / end /
        # mode fields. Leaving both start and end blank disables that slot.
        schedule_fields = _build_schedule_slot_schema(self.config_entry.options)
        schema_fields.update(schedule_fields)

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


# ---------------------------------------------------------------------------
# #359 schedule-window slot helpers
# ---------------------------------------------------------------------------


def _build_schedule_slot_schema(current_options: Mapping[str, Any]) -> dict[Any, Any]:
    """Build the ``vol.Optional`` fields for each schedule slot (#359).

    Each slot contributes three fields to the options form: a start-time picker,
    an end-time picker, and a mode dropdown. Defaults are pulled from the
    entry's currently-persisted ``CONF_SCHEDULE_WINDOWS`` (if any) so re-opening
    the options form shows the values the user last submitted.
    """
    from homeassistant.helpers.selector import (
        SelectOptionDict,
        SelectSelector,
        SelectSelectorConfig,
        TimeSelector,
    )

    current_windows = list(current_options.get(CONF_SCHEDULE_WINDOWS) or [])
    fields: dict[Any, Any] = {}
    for slot in range(1, _SCHEDULE_SLOTS + 1):
        row = current_windows[slot - 1] if slot - 1 < len(current_windows) else {}
        start_default = str(row.get("start") or "")
        end_default = str(row.get("end") or "")
        mode_default = str(row.get("mode") or _SCHEDULE_MODE_OPTIONS[0])
        # ``description={"suggested_value": ...}`` keeps the field empty by default
        # in the UI but pre-fills the last value on re-open; ``vol.Optional`` with an
        # empty-string default lets the user *clear* the slot to disable it.
        fields[
            vol.Optional(
                f"schedule_{slot}_start",
                description={"suggested_value": start_default} if start_default else None,
            )
        ] = TimeSelector()
        fields[
            vol.Optional(
                f"schedule_{slot}_end",
                description={"suggested_value": end_default} if end_default else None,
            )
        ] = TimeSelector()
        fields[
            vol.Optional(
                f"schedule_{slot}_mode",
                default=mode_default,
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value="force_charge", label="Force charge"),
                    SelectOptionDict(value="force_discharge", label="Force discharge"),
                ]
            )
        )
    return fields


def _collect_schedule_windows(
    user_input: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Read the per-slot fields back into a normalised ``CONF_SCHEDULE_WINDOWS`` list.

    Returns ``(windows, errors)``: a slot with both start and end blank is treated
    as "disabled" and simply omitted; a half-populated slot (only one of start /
    end set) is a user error surfaced as ``invalid_schedule_window`` so they can
    fix it. Overlapping windows are allowed at save time — the engine resolves
    overlaps by picking the latest-starting one.
    """
    windows: list[dict[str, str]] = []
    errors: dict[str, str] = {}
    for slot in range(1, _SCHEDULE_SLOTS + 1):
        start = (user_input.get(f"schedule_{slot}_start") or "").strip()
        end = (user_input.get(f"schedule_{slot}_end") or "").strip()
        mode = (user_input.get(f"schedule_{slot}_mode") or "").strip()
        if not start and not end:
            continue  # slot disabled — legitimate
        if not start or not end:
            errors["base"] = "invalid_schedule_window"
            _LOGGER.warning("Schedule slot %d has only one of start/end filled in", slot)
            continue
        if mode not in _SCHEDULE_MODE_OPTIONS:
            errors["base"] = "invalid_schedule_window"
            _LOGGER.warning("Schedule slot %d has an unknown mode %r", slot, mode)
            continue
        windows.append({"start": start, "end": end, "mode": mode})
    return windows, errors
