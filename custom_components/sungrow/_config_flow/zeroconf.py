"""WiNet-S zeroconf discovery for the cloud-free Modbus transport (#354).

The dongle advertises ``WiNet-WebServer`` (``_http._tcp``) with TXT records that
carry the inverter serial and model, so we can identify it and pick the register
map without connecting or needing any cloud credentials.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from ..const import (
    CONF_DISCOVERY_MANAGED_HOST,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DOMAIN,
    TRANSPORT_MODBUS_ONLY,
)
from ._base import _SungrowFlowBase
from ._helpers import _parse_winet_properties


class ZeroconfMixin(_SungrowFlowBase):
    """WiNet-S zeroconf discovery steps for :class:`SungrowConfigFlow`."""

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Discover a WiNet-S dongle via mDNS and offer a cloud-free local Modbus setup (#159).

        The dongle advertises ``WiNet-WebServer`` (``_http._tcp``) with TXT records that
        carry the inverter's serial and model, so we can identify it and pick the register
        map without connecting or needing any cloud credentials.
        """
        host = str(discovery_info.ip_address)
        serial, model = _parse_winet_properties(discovery_info.properties)
        if not serial:
            return self.async_abort(reason="not_sungrow_device")
        unique_id = f"modbus_{serial}"
        await self.async_set_unique_id(unique_id)
        # Already configured? Abort — and only refresh the stored host when it came from
        # discovery and the user never changed it (the dongle may have moved to a new
        # DHCP lease). A host the user chose explicitly, e.g. the inverter's dedicated
        # RJ45 Modbus TCP port, must never be overwritten by the WiNet-S address we
        # happened to discover (#402).
        existing = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
        updates = (
            {CONF_MODBUS_HOST: host}
            if existing is not None and existing.data.get(CONF_DISCOVERY_MANAGED_HOST)
            else None
        )
        self._abort_if_unique_id_configured(updates=updates)
        self._discovered_modbus_host = host
        self.init_info = {CONF_SERIAL: serial, CONF_MODEL: model or "Inverter"}
        self.context["title_placeholders"] = {"name": f"Sungrow {model or 'inverter'}"}
        # Always a standalone local entry — never mash Modbus into the cloud entry.
        # If a cloud plant already owns this serial, setup nests the local inverter under
        # that plant via device registry (soft link only).
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm setting up the discovered WiNet-S as a local (Modbus-only) integration."""
        model = self.init_info.get(CONF_MODEL, "Inverter")
        if user_input is not None:
            return self.async_create_entry(
                title=f"Sungrow {model} (local)",
                data={
                    CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                    CONF_SERIAL: self.init_info[CONF_SERIAL],
                    CONF_MODEL: model,
                    CONF_MODBUS_HOST: self._discovered_modbus_host,
                    # The host came from discovery, so a later re-discovery may follow a
                    # moved dongle — until the user sets the host explicitly (#402).
                    CONF_DISCOVERY_MANAGED_HOST: True,
                },
                options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"model": model, "host": self._discovered_modbus_host or ""},
        )
