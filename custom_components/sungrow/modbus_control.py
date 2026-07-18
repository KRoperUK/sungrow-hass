"""Local Modbus dispatch control via WiNet-S holding registers (#220).

Duck-types the same surface as ``Control`` / ``UserControl`` so number/select
entities can write without a cloud account. Phase 1 maps only validated SG-RS
active-power holdings (wire 5006/5007); unmapped params raise clearly.
"""

from __future__ import annotations

import logging
from typing import Any

from .modbus import SungrowModbusClient, SungrowModbusError
from .modbus_registers import (
    HOLDING_CONTROL_MAPS,
    HOLDING_WRITE_DENYLIST_WIRE,
    HoldingControlPoint,
)

_LOGGER = logging.getLogger(__name__)


class ModbusControlError(Exception):
    """A local Modbus control read or write failed."""


class ModbusControl:
    """Dispatch client backed by family-gated holding-register maps."""

    def __init__(self, client: SungrowModbusClient, *, family: str | None = None) -> None:
        self._client = client
        self.family = family or client.model or "sg_rs"
        points = HOLDING_CONTROL_MAPS.get(self.family, ())
        self._by_param: dict[str, HoldingControlPoint] = {p.param: p for p in points}
        self._by_code: dict[str, HoldingControlPoint] = {p.param_code: p for p in points if p.param_code is not None}
        # Entity builders use this to hide unmapped cloud controls on local entries.
        self.supported_parameters: frozenset[str] = frozenset(self._by_param)

    def _resolve(self, name_or_code: str) -> HoldingControlPoint:
        key = str(name_or_code)
        point = self._by_param.get(key) or self._by_code.get(key)
        if point is None:
            raise ModbusControlError(f"Parameter {key!r} is not available over local Modbus on family {self.family!r}")
        if point.wire_address in HOLDING_WRITE_DENYLIST_WIRE:
            raise ModbusControlError(f"Parameter {key!r} maps to denylisted wire {point.wire_address}")
        return point

    async def async_check_update_support(self, device_uuid: str) -> bool:
        """Return True when the primary holding control map is readable."""
        if not self._by_param:
            return False
        # Prefer ratio register; fall back to any mapped point.
        probe = self._by_param.get("active_power_limit_ratio") or next(iter(self._by_param.values()))
        try:
            await self._client.async_read_holding(probe.wire_address, 1)
        except SungrowModbusError as err:
            _LOGGER.debug("Modbus control support check failed for %s: %s", device_uuid, err)
            return False
        return True

    async def async_read_parameters(
        self, device_uuid: str, param_list: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Read mapped holdings; returns cloud-shaped readout dicts."""
        names = list(param_list) if param_list is not None else list(self._by_param)
        out: list[dict[str, Any]] = []
        for name in names:
            point = self._resolve(name)
            try:
                regs = await self._client.async_read_holding(point.wire_address, 1)
            except SungrowModbusError as err:
                raise ModbusControlError(f"Read {point.param} failed: {err}") from err
            raw = int(regs[0])
            out.append(
                {
                    "id": point.param_code or point.param,
                    "code": point.param,
                    "name": point.description,
                    "value": str(raw),
                    "unit": "",
                    "precision": None,
                }
            )
        return out

    async def async_update_parameters(self, device_uuid: str, param_values: dict[str, Any]) -> list[dict[str, Any]]:
        """Write cloud-encoded parameter values to holding registers with read-back."""
        out: list[dict[str, Any]] = []
        for name, value in param_values.items():
            point = self._resolve(str(name))
            try:
                wire = int(str(value).strip())
            except (TypeError, ValueError) as err:
                raise ModbusControlError(f"Invalid wire value for {point.param}: {value!r}") from err
            if not 0 <= wire <= 0xFFFF:
                raise ModbusControlError(f"Wire value out of U16 range for {point.param}: {wire}")
            try:
                await self._client.async_write_holding(point.wire_address, wire)
                verify = await self._client.async_read_holding(point.wire_address, 1)
            except SungrowModbusError as err:
                raise ModbusControlError(f"Write {point.param} failed: {err}") from err
            vraw = int(verify[0])
            if vraw != wire:
                raise ModbusControlError(f"Write {point.param} read-back mismatch: wrote {wire}, read {vraw}")
            _LOGGER.debug(
                "Modbus wrote %s=%s to %s wire %s (device %s)",
                point.param,
                wire,
                self.family,
                point.wire_address,
                device_uuid,
            )
            out.append(
                {
                    "id": point.param_code or point.param,
                    "code": point.param,
                    "name": point.description,
                    "value": str(vraw),
                    "unit": "",
                    "precision": None,
                }
            )
        return out

    async def async_set_parameter(self, device_uuid: str, name: str, value: object) -> list[dict[str, Any]]:
        """Encode via cloud Control specs then write (for callers that pass display values)."""
        from pysolarcloud.control import Control

        wire = Control.encode_parameter(name, value)
        return await self.async_update_parameters(device_uuid, {name: wire})
