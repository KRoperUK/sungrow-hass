"""Modbus-only (cloud-free) transport steps (#354, #374).

Covers direct-Modbus setup without any iSolarCloud credentials via a guided wizard
(#374):

- ``async_step_local_discovery`` — scan for WiNet-S dongles on the LAN, present a
  picker (plus an explicit "Enter IP manually" option and a "Rescan" action). The
  entry point when the user selects **Modbus Only** from the transport selector.
- ``async_step_local_manual_ip`` — text-field IP + port entry with a TCP reachability
  probe (port defaults to 502; a custom port reaches a Modbus proxy, #485). Reached when nothing was discovered or the user opted out of the picker.
- ``async_step_local_confirm_identified`` — final confirmation once we have a
  reachable host plus detected model/serial. Runs a real Modbus read as the
  create-entry gate so a comms failure surfaces here, not at the first refresh.
- ``async_step_local_setup`` — the pre-#374 manual form, retained as a fallback
  for the "identify failed" branch (fields pre-filled with anything we did learn)
  and as a compatibility alias for external docs / SOURCE_IMPORT.
- ``async_step_import`` — programmatic entry creation (legacy hybrid split).
- ``async_step_reconfigure_modbus`` — update the WiNet-S host/port on an existing entry.

Zeroconf-driven discovery (a WiNet-S announcing itself while the user is on the HA
Discovered card, not inside a manual flow) still lives in :mod:`.zeroconf`.

Entries stay keyed on ``modbus_{serial}``, not host/port (#485): one entry per
inverter, however it is reached. Several inverters behind one proxy on different
ports have different serials, so each still gets its own entry; the same inverter
reached via a second host/port updates the existing entry instead of duplicating it.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig

from ..const import (
    CONF_DISCOVERY_MANAGED_HOST,
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_PORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    TRANSPORT_MODBUS_ONLY,
)
from ..helpers import resolve_modbus_port
from ._base import _SungrowFlowBase
from ._helpers import (
    MODBUS_PORT_VALIDATOR,
    WinetDongle,
    async_discover_winet_dongles,
    async_read_modbus_identity,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel values for the discovery picker so the SelectSelector can carry
# non-host actions alongside actual dongle hosts. Plain identifier-style
# tokens so they can't collide with a legitimate IP (has dots) or hostname
# (typically has dots too, and can't be an underscore-only word). Also legal
# translation keys under HA's ``[a-z0-9-_]+, not leading/trailing hyphen/
# underscore`` rule for the ``selector.local_discovery.options.*`` block.
_DISCOVERY_MANUAL = "manual_ip"
_DISCOVERY_RESCAN = "rescan"


def _pinned_host_data(host: str, port: int) -> dict[str, Any]:
    """Entry-data fields that pin the host (and port) to a user-chosen value.

    Clearing ``CONF_DISCOVERY_MANAGED_HOST`` marks the host as deliberately chosen, so a
    later WiNet-S discovery must not overwrite it with the dongle's address (#402) —
    which matters doubly behind a Modbus proxy, whose host is not the dongle's (#485).
    """
    return {CONF_MODBUS_HOST: host, CONF_MODBUS_PORT: port, CONF_DISCOVERY_MANAGED_HOST: False}


def _host_label(host: str, port: int) -> str:
    """Render ``host`` for a form description, adding the port only when non-standard."""
    return host if port == DEFAULT_MODBUS_PORT else f"{host}:{port}"


class ModbusOnlyMixin(_SungrowFlowBase):
    """Modbus-only setup / import / reconfigure steps for :class:`SungrowConfigFlow`."""

    # ------------------------------------------------------------------
    # Guided wizard (#374)
    # ------------------------------------------------------------------

    async def async_step_local_discovery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Scan for WiNet-S dongles and present a picker (entry step for Modbus Only).

        The picker holds one option per discovered dongle plus two synthetic actions:
        ``manual_ip`` (user wants to type an IP) and ``rescan`` (re-run this
        step). Selecting an actual dongle carries its ``host`` / ``serial`` / ``model``
        into :meth:`async_step_local_confirm_identified` without a Modbus round-trip;
        we still gate creation on a real Modbus read there so we never build an entry
        that can't talk to the inverter.
        """
        # Handle a submitted picker choice from an earlier render of this step.
        if user_input is not None:
            choice = user_input.get("choice")
            if choice == _DISCOVERY_RESCAN:
                # Force a fresh browse — clear any cached candidates.
                self._discovered_winet_dongles = None
                return await self.async_step_local_discovery()
            if choice == _DISCOVERY_MANUAL or not choice:
                return await self.async_step_local_manual_ip()
            # Otherwise the choice is a discovered host — look it up in the cache.
            dongle = self._winet_dongle_by_host(choice)
            if dongle is not None:
                self._local_wizard_host = dongle.host
                # A dongle found on the LAN is the WiNet-S itself, so it speaks on 502.
                self._local_wizard_port = None
                self._local_wizard_serial = dongle.serial
                self._local_wizard_model = dongle.model
                return await self.async_step_local_confirm_identified()
            # Cache miss (stale render across a Home Assistant restart, say): fall
            # through to a fresh scan.

        # Fresh (or cached) discovery. Cache the last successful scan so the picker
        # survives an intermediate "confirm" back-navigation without re-scanning.
        if self._discovered_winet_dongles is None:
            dongles = await async_discover_winet_dongles(self.hass)
            self._discovered_winet_dongles = dongles
        else:
            dongles = self._discovered_winet_dongles

        options: list[SelectOptionDict] = [
            SelectOptionDict(value=d.host, label=self._format_dongle_label(d)) for d in dongles
        ]
        options.append(SelectOptionDict(value=_DISCOVERY_MANUAL, label="Enter IP manually"))
        options.append(SelectOptionDict(value=_DISCOVERY_RESCAN, label="Rescan"))

        default = options[0]["value"] if dongles else _DISCOVERY_MANUAL

        return self.async_show_form(
            step_id="local_discovery",
            data_schema=vol.Schema(
                {
                    vol.Required("choice", default=default): SelectSelector(
                        SelectSelectorConfig(options=options, translation_key="local_discovery")
                    ),
                }
            ),
            description_placeholders={"count": str(len(dongles))},
        )

    async def async_step_local_manual_ip(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect a WiNet-S host + port manually, probe TCP, then hand off to identify.

        Reached from the discovery picker's ``manual_ip`` option (or automatically
        when discovery finds nothing). On a reachable host we try to read the model
        + serial via :func:`async_read_modbus_identity`; a full identify goes to the
        confirmation step, a partial or complete identify miss falls through to
        :meth:`async_step_local_setup` with whatever we did learn pre-filled.

        The port defaults to 502 (WiNet-S / inverter LAN); a different one reaches a
        Modbus proxy such as evcc's ``modbusproxy`` (#485). Both the probe and the
        identify read use it.
        """
        errors: dict[str, str] = {}
        default_host = self._local_wizard_host or ""
        default_port = self._local_wizard_port or DEFAULT_MODBUS_PORT

        if user_input is not None:
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
            port = int(user_input.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT))
            default_host = host
            default_port = port
            from ..helpers import async_test_modbus_host

            if not host or not await async_test_modbus_host(host, port):
                errors["base"] = "host_unreachable"
            else:
                self._local_wizard_host = host
                self._local_wizard_port = port
                # Attempt identity read. Partial or total failure just means the manual
                # form takes over with what we have.
                model, serial = await async_read_modbus_identity(host, port)
                if serial:
                    self._local_wizard_serial = serial
                if model:
                    self._local_wizard_model = model
                if model and serial:
                    return await self.async_step_local_confirm_identified()
                # Identify was partial or missed entirely — fall through to the manual
                # form so the user can supply the missing pieces.
                return await self.async_step_local_setup()

        return self.async_show_form(
            step_id="local_manual_ip",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=default_host): str,
                    vol.Optional(CONF_MODBUS_PORT, default=default_port): MODBUS_PORT_VALIDATOR,
                },
            ),
            errors=errors,
        )

    async def async_step_local_confirm_identified(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show detected model/serial/host, gate creation on a real comms probe.

        Submitting the form runs :func:`async_read_modbus_identity` once more as the
        create-entry probe. That double-checks the wire is still reachable and the
        inverter still answers with the same serial (guards against picking up a
        stale zeroconf cache entry for a dongle that has since gone offline). On
        success we build the entry; on failure we route back to the manual IP step
        with an error so the user can retry or pick a different host.
        """
        host = self._local_wizard_host
        port = self._local_wizard_port or DEFAULT_MODBUS_PORT
        model = self._local_wizard_model or "Inverter"
        serial = self._local_wizard_serial

        if host is None or serial is None:
            # Shouldn't happen — the wizard only reaches this step with both set —
            # but if state has been dropped (e.g. HA restart mid-flow) rewind to
            # discovery rather than creating a partially-populated entry.
            return await self.async_step_local_discovery()

        placeholders = {"host": _host_label(host, port), "model": model, "serial": serial}
        if user_input is not None:
            # Final comms probe: re-read identity, confirm the serial still matches.
            # We accept a partial re-read (missing model on second read) but reject a
            # serial mismatch — that would be a different device on the same host.
            reread_model, reread_serial = await async_read_modbus_identity(host, port)
            if reread_serial is None:
                return self.async_show_form(
                    step_id="local_confirm_identified",
                    description_placeholders=placeholders,
                    errors={"base": "comms_probe_failed"},
                )
            if reread_serial != serial:
                _LOGGER.warning(
                    "Comms probe returned a different serial (%s) than discovery (%s); refusing to add",
                    reread_serial,
                    serial,
                )
                return self.async_show_form(
                    step_id="local_confirm_identified",
                    description_placeholders=placeholders,
                    errors={"base": "serial_mismatch"},
                )
            await self.async_set_unique_id(f"modbus_{serial}")
            self._abort_if_unique_id_configured(updates=_pinned_host_data(host, port))
            return self.async_create_entry(
                title=f"Sungrow {model} (local)",
                data={
                    CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                    CONF_SERIAL: serial,
                    CONF_MODEL: model,
                    CONF_MODBUS_HOST: host,
                    CONF_MODBUS_PORT: port,
                },
                options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
            )

        return self.async_show_form(
            step_id="local_confirm_identified",
            description_placeholders=placeholders,
            data_schema=vol.Schema({}),
        )

    # ------------------------------------------------------------------
    # Fallback + legacy manual entry
    # ------------------------------------------------------------------

    async def async_step_local_setup(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual host/serial/model form — the fallback when identify can't fill it in.

        Reached when :meth:`async_step_local_manual_ip` succeeds on reachability but
        the follow-up Modbus identity read only produced partial (or no) results.
        Fields are pre-filled with whatever the wizard did learn, so the user only
        has to fill the gaps. Behaviour on submit is unchanged from the pre-#374
        form: reachability check + create entry.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip()
            serial = (user_input.get(CONF_SERIAL) or "").strip()
            model = (user_input.get(CONF_MODEL) or "Inverter").strip()
            port = int(user_input.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT))

            from ..helpers import async_test_modbus_host

            if await async_test_modbus_host(host, port):
                await self.async_set_unique_id(f"modbus_{serial}")
                self._abort_if_unique_id_configured(updates=_pinned_host_data(host, port))
                return self.async_create_entry(
                    title=f"Sungrow {model} (local)",
                    data={
                        CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                        CONF_SERIAL: serial,
                        CONF_MODEL: model,
                        CONF_MODBUS_HOST: host,
                        CONF_MODBUS_PORT: port,
                    },
                    options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
                )
            errors["base"] = "host_unreachable"

        return self.async_show_form(
            step_id="local_setup",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=self._local_wizard_host or ""): str,
                    vol.Optional(
                        CONF_MODBUS_PORT, default=self._local_wizard_port or DEFAULT_MODBUS_PORT
                    ): MODBUS_PORT_VALIDATOR,
                    vol.Required(CONF_SERIAL, default=self._local_wizard_serial or ""): str,
                    vol.Required(CONF_MODEL, default=self._local_wizard_model or "Inverter"): str,
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
        # A legacy hybrid entry may have carried a non-standard ``modbus_port`` in its
        # options; keep it so the split-off local entry still reaches the same endpoint.
        try:
            port = int(MODBUS_PORT_VALIDATOR(user_input.get(CONF_MODBUS_PORT) or DEFAULT_MODBUS_PORT))
        except vol.Invalid:
            port = DEFAULT_MODBUS_PORT
        await self.async_set_unique_id(f"modbus_{serial}")
        self._abort_if_unique_id_configured(updates=_pinned_host_data(host, port))
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
                CONF_MODBUS_PORT: port,
            },
            options=options,
        )

    async def async_step_reconfigure_modbus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Reconfigure a cloud-free Modbus-only entry: update the host and port (#159, #485).

        No credentials are involved — what is worth changing is the local endpoint: a
        new DHCP lease the discovery did not re-announce, the inverter's own RJ45 port,
        or a Modbus proxy on a custom port (#485). The new endpoint must answer on TCP
        before it is saved, so a typo cannot silently take the entry offline. Saving
        clears any options-flow port override so the value entered here is the one used.
        """
        entry = self._get_reconfigure_entry()
        current_host = str(entry.data.get(CONF_MODBUS_HOST) or "")
        current_port = resolve_modbus_port(entry.options, entry.data)
        errors: dict[str, str] = {}
        if user_input is not None:
            # Blank means "leave unchanged" so reconfigure can never accidentally clear
            # the host (which would take the entry offline).
            host = (user_input.get(CONF_MODBUS_HOST) or "").strip() or current_host
            port = int(user_input.get(CONF_MODBUS_PORT, current_port))
            from ..helpers import async_test_modbus_host

            if host and await async_test_modbus_host(host, port):
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **_pinned_host_data(host, port)},
                    options={k: v for k, v in entry.options.items() if k != CONF_MODBUS_PORT},
                    reason="reconfigure_successful",
                )
            errors["base"] = "host_unreachable"
            current_host, current_port = host, port
        return self.async_show_form(
            step_id="reconfigure_modbus",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODBUS_HOST, default=current_host): str,
                    vol.Optional(CONF_MODBUS_PORT, default=current_port): MODBUS_PORT_VALIDATOR,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_dongle_label(d: WinetDongle) -> str:
        """Render a picker label for one discovered dongle.

        Prefer the model name over the raw serial when the TXT record carries it,
        falling back to whatever we have; end with the host so the user can pick a
        specific unit even when two share the same model.
        """
        head = d.model or "Sungrow inverter"
        if d.serial:
            head = f"{head} · {d.serial}"
        return f"{head} ({d.host})"

    def _winet_dongle_by_host(self, host: str) -> WinetDongle | None:
        """Look up a previously-discovered dongle by its host, if the cache is fresh."""
        if not self._discovered_winet_dongles:
            return None
        for dongle in self._discovered_winet_dongles:
            if dongle.host == host:
                return dongle
        return None
