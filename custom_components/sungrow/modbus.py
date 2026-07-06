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

from .modbus_registers import REGISTER_MAPS, block_bounds, decode_registers

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

    async def async_read_realtime(self) -> dict[str, dict[str, Any]]:
        """Read and decode the model's realtime input registers.

        Returns ``{code: {"code", "value", "unit", "source": "modbus"}}``. Raises
        :class:`SungrowModbusError` on connection/read failure so the caller can fall
        back to the cloud transport.
        """
        points = REGISTER_MAPS.get(self.model)
        if not points:
            raise SungrowModbusError(f"No Modbus register map for model {self.model!r}")
        start, count = block_bounds(points)
        async with self._lock:
            registers = await self._read_input(start, count)
        return decode_registers(points, start, registers)

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
