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
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
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
        await self.async_set_unique_id(f"modbus_{serial}")
        # Already set up? Update the host in case the WiNet-S's IP changed, then stop.
        self._abort_if_unique_id_configured(updates={CONF_MODBUS_HOST: host})
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
                },
                options={CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"model": model, "host": self._discovered_modbus_host or ""},
        )
