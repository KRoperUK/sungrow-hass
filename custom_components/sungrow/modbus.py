"""Local Modbus transport for Sungrow inverters via the WiNet-S dongle (#159).

Reads realtime data straight from the inverter over Modbus TCP instead of (or
alongside) the iSolarCloud API — fast (~10 ms), unmetered, and offline-capable.
Holds a single persistent connection and serialises reads: the WiNet-S is happiest
with one Modbus conversation at a time. The decoded output matches the cloud
transport's ``{code: {value, unit, ...}}`` shape (tagged ``source="modbus"``) so both
sources feed the same entities.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pymodbus.client import AsyncModbusTcpClient

from .modbus_registers import (
    DAILY_YIELD_DIAG_COUNT,
    DAILY_YIELD_DIAG_START,
    REGISTER_MAPS,
    block_partitions,
    daily_yield_diagnostic_dump,
    decode_registers,
    family_for_device_type_code,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 502
DEFAULT_UNIT = 1
CONNECT_TIMEOUT = 5


class SungrowModbusError(Exception):
    """A local Modbus connection or read failed."""


class SungrowModbusClient:
    """Read realtime data from one Sungrow inverter over local Modbus TCP."""

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        unit: int = DEFAULT_UNIT,
        model: str = "sg_rs",
    ) -> None:
        """Initialize the client for a WiNet-S host (one inverter)."""
        self.host = host
        self.port = port
        self.unit = unit
        self.model = model
        self._client = AsyncModbusTcpClient(host, port=port, timeout=CONNECT_TIMEOUT)
        # Serialise reads onto the single connection the WiNet-S expects.
        self._lock = asyncio.Lock()
        self._family_detected = False

    async def async_read_realtime(self) -> dict[str, dict[str, Any]]:
        """Read and decode the model's realtime input registers.

        On the first call the inverter family is auto-detected from register 5000
        (device_type_code) when the code is known, falling back to the configured
        ``model`` otherwise.

        Returns ``{code: {"code", "value", "unit", "source": "modbus"}}``. Raises
        :class:`SungrowModbusError` on connection/read failure so the caller can fall
        back to the cloud transport.
        """
        await self._async_ensure_family()
        points = REGISTER_MAPS.get(self.model)
        if not points:
            raise SungrowModbusError(f"No Modbus register map for model {self.model!r}")
        out: dict[str, dict[str, Any]] = {}
        async with self._lock:
            for start, count in block_partitions(points):
                registers = await self._read_input(start, count)
                out.update(decode_registers(points, start, registers))
        return out

    async def async_read_daily_yield_diagnostic(self) -> dict[str, Any]:
        """Read the wire-register window around the daily_yield register and return a #223 diagnostic.

        Reads only the diagnostic window (4999..5010), so the cost is one extra short
        Modbus request per poll. The result is the value surfaced on the
        ``daily_yield`` sensor's ``daily_yield_diagnostic`` attribute and includes the
        raw 16-bit values plus every plausible ``(address, scale)`` decoding so a
        daytime re-capture (#223) can pick the right mapping without guessing.

        Raises :class:`SungrowModbusError` on connection/read failure so the caller can
        simply leave the previous diagnostic in place.
        """
        async with self._lock:
            registers = await self._read_input(DAILY_YIELD_DIAG_START, DAILY_YIELD_DIAG_COUNT)
        return daily_yield_diagnostic_dump(registers, DAILY_YIELD_DIAG_START)

    async def _async_ensure_family(self) -> None:
        """Detect the inverter family once from register 5000 and update ``model``."""
        if self._family_detected:
            return
        self._family_detected = True
        try:
            async with self._lock:
                registers = await self._read_input(4999, 1)
            code = int(registers[0])
            family = family_for_device_type_code(code)
            if family is not None:
                _LOGGER.debug("Device-type code %s mapped to Modbus family %s", code, family)
                self.model = family
                return
            _LOGGER.debug("Unknown device-type code %s; keeping configured model %s", code, self.model)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not auto-detect Modbus family: %s", err)

    async def _read_input(self, address: int, count: int) -> list[int]:
        """Read a contiguous input-register block, connecting/reconnecting as needed."""
        if not self._client.connected and not await self._client.connect():
            raise SungrowModbusError(f"Could not connect to {self.host}:{self.port}")
        result = await self._client.read_input_registers(address, count=count, device_id=self.unit)
        if result.isError():
            # Drop the connection so the next read reconnects cleanly.
            self._client.close()
            raise SungrowModbusError(f"Modbus read at {address} (count {count}) failed: {result}")
        return list(result.registers)

    def close(self) -> None:
        """Close the underlying connection."""
        self._client.close()
