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
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from .modbus_registers import (
    DAILY_YIELD_DIAG_COUNT,
    DAILY_YIELD_DIAG_START,
    REGISTER_MAPS,
    ModbusPoint,
    block_partitions,
    daily_yield_diagnostic_dump,
    decode_registers,
    family_for_device_type_code,
    suppress_absent_meter_points,
)
from .model_capabilities import ModelFamily, resolve_model_family
from .model_specs import spec_for

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 502
DEFAULT_UNIT = 1
CONNECT_TIMEOUT = 5
# One reconnect+retry after a dropped WiNet-S socket (common after options reload).
_READ_ATTEMPTS = 2


def _points_for_model(points: tuple[ModbusPoint, ...], model_code: str, family: str) -> tuple[ModbusPoint, ...]:
    """Return MPPT points supported by a known model in the detected family."""
    spec = spec_for(model_code)
    if spec is None or resolve_model_family(model_code).value != family:
        return points

    selected: list[ModbusPoint] = []
    for point in points:
        prefix, separator, _ = point.code.partition("_")
        tracker = prefix.removeprefix("mppt")
        if not separator or not prefix.startswith("mppt") or not tracker.isdigit():
            selected.append(point)
            continue
        if int(tracker) > spec.mppt_count:
            continue
        selected.append(replace(point, omit_zero=False) if point.omit_zero else point)
    return tuple(selected)


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
        model_code: str | None = None,
    ) -> None:
        """Initialize the client for a WiNet-S host (one inverter)."""
        self.host = host
        self.port = port
        self.unit = unit
        self.model = model
        self._configured_model = model_code or model
        self._client = self._new_tcp_client()
        # Serialise reads onto the single connection the WiNet-S expects.
        self._lock = asyncio.Lock()
        self._family_detected = False
        self.modbus_diagnostics: dict[str, Any] = {
            "device_family": None,
            "skipped_blocks": [],
            "last_error": None,
        }

    def _new_tcp_client(self) -> AsyncModbusTcpClient:
        return AsyncModbusTcpClient(self.host, port=self.port, timeout=CONNECT_TIMEOUT)

    async def async_read_realtime(self) -> dict[str, dict[str, Any]]:
        """Read and decode the model's realtime input registers.

        On the first call the inverter family is auto-detected from register 5000
        (device_type_code) when the code is known, falling back to the configured
        ``model`` otherwise.

        Returns ``{code: {"code", "value", "unit", "source": "modbus"}}``. Raises
        :class:`SungrowModbusError` on connection/read failure so the caller can fall
        back to the cloud transport.
        """
        self.modbus_diagnostics["skipped_blocks"] = []
        self.modbus_diagnostics["last_error"] = None
        await self._async_ensure_family()
        self.modbus_diagnostics["device_family"] = self.model
        points = REGISTER_MAPS.get(self.model)
        if not points:
            raise SungrowModbusError(f"No Modbus register map for model {self.model!r}")
        points = _points_for_model(points, self._configured_model, self.model)
        out: dict[str, dict[str, Any]] = {}
        async with self._lock:
            for start, count in block_partitions(points):
                try:
                    registers = await self._read_input(start, count)
                except SungrowModbusError as err:
                    self.modbus_diagnostics["last_error"] = str(err)
                    # Exception code 2 = Illegal Data Address: the inverter firmware
                    # does not implement any register in this block (e.g. high energy
                    # registers on a string inverter). Skip the block rather than fail
                    # the whole poll; if every block fails the caller still sees an
                    # empty result and can retry on the next cycle.
                    if _is_unsupported_address(err):
                        _LOGGER.debug("Skipping unsupported Modbus block at %s (count %s): %s", start, count, err)
                        self.modbus_diagnostics["skipped_blocks"].append({"start": start, "count": count})
                        continue
                    raise
                out.update(decode_registers(points, start, registers))
        # An uncommissioned grid meter answers 0 instead of the "not available"
        # sentinel, which would otherwise feed the Energy dashboard a permanent and
        # entirely plausible 0 kWh of import/export (#387).
        out, meter_present = suppress_absent_meter_points(out)
        self.modbus_diagnostics["meter_present"] = meter_present
        if not meter_present:
            _LOGGER.debug(
                "No external grid meter detected for %s; suppressing zero-valued "
                "meter-derived points (load/import/export)",
                self.host,
            )
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
        # Prefer a known device-type code; else resolve the configured model string
        # (e.g. SH10RT-20 → sh_rt) when it already names a register map (#219).
        configured = self._configured_model
        try:
            async with self._lock:
                registers = await self._read_input(4999, 1)
            code = int(registers[0])
            family = family_for_device_type_code(code)
            if family is not None:
                _LOGGER.debug("Device-type code %s mapped to Modbus family %s", code, family)
                self.model = family
                return
            _LOGGER.debug("Unknown device-type code %s; trying model string %s", code, configured)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not auto-detect Modbus family from register: %s", err)

        resolved = resolve_model_family(configured)
        if resolved is not ModelFamily.UNKNOWN and resolved.value in REGISTER_MAPS:
            _LOGGER.debug("Model string %s resolved to Modbus family %s", configured, resolved.value)
            self.model = resolved.value

    async def _async_ensure_connected(self) -> None:
        """Connect if needed; recreate the TCP client when a prior session is dead."""
        if self._client.connected:
            return
        if await self._client.connect():
            return
        # pymodbus can leave a closed client in a state where connect() never recovers;
        # drop it and open a fresh socket (observed after options reload / diag dump).
        _LOGGER.debug("Modbus connect failed for %s:%s; recreating client", self.host, self.port)
        self._recreate_client()
        if not await self._client.connect():
            raise SungrowModbusError(f"Could not connect to {self.host}:{self.port}")

    def _recreate_client(self) -> None:
        """Close the socket and replace the pymodbus client.

        Always construct a new client: after ``close()`` some pymodbus builds still report
        ``connected=True``, which would skip reconnect forever and leave setup stuck on
        ``Not connected`` after options reload (e.g. toggling the daily-yield diagnostic).
        """
        with contextlib.suppress(Exception):
            self._client.close()
        self._client = self._new_tcp_client()

    async def _read_input(self, address: int, count: int) -> list[int]:
        """Read a contiguous input-register block, reconnecting once on connection loss."""
        return await self._transact_read(
            "input",
            address,
            count,
            lambda: self._client.read_input_registers(address, count=count, device_id=self.unit),
        )

    async def async_read_holding(self, address: int, count: int = 1) -> list[int]:
        """Read holding registers (FC3) for config/control probes and future #220 writes."""
        async with self._lock:
            return await self._transact_read(
                "holding",
                address,
                count,
                lambda: self._client.read_holding_registers(address, count=count, device_id=self.unit),
            )

    async def async_write_holding(self, address: int, value: int) -> None:
        """Write a single holding register (FC6).

        Callers must enforce family maps and the write denylist; this method only
        performs the transport. Serialised with the same lock as reads.
        """
        if not 0 <= int(value) <= 0xFFFF:
            raise SungrowModbusError(f"Holding write value out of U16 range: {value}")

        async def _do_write() -> Any:
            return await self._client.write_register(address, int(value), device_id=self.unit)

        async with self._lock:
            await self._transact_write("holding_write", address, _do_write)

    async def _transact_read(
        self,
        kind: str,
        address: int,
        count: int,
        operation: Callable[[], Awaitable[Any]],
    ) -> list[int]:
        """Run a register read with one reconnect+retry; return raw 16-bit values."""
        result = await self._transact(kind, address, count, operation)
        registers = getattr(result, "registers", None)
        if registers is None:
            raise SungrowModbusError(f"Modbus {kind} at {address} returned no registers: {result}")
        return [int(r) for r in registers]

    async def _transact_write(
        self,
        kind: str,
        address: int,
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        """Run a single-register write with one reconnect+retry."""
        await self._transact(kind, address, 1, operation)

    async def _transact(
        self,
        kind: str,
        address: int,
        count: int,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run a Modbus operation with one reconnect+retry on connection loss.

        WiNet-S sockets drop after idle, options reload, or a mid-poll error. A stale
        ``connected`` flag or a closed transport that never recovers from ``connect()``
        produces ``ConnectionException: Not connected[...]`` until the client is recreated.
        """
        last_error: SungrowModbusError | None = None
        for attempt in range(_READ_ATTEMPTS):
            try:
                await self._async_ensure_connected()
                result = await operation()
                if hasattr(result, "isError") and result.isError():
                    msg = f"Modbus {kind} at {address} (count {count}) failed: {result}"
                    # Illegal address is permanent for this block — do not retry.
                    if _is_exception_code(str(result), 2):
                        self._recreate_client()
                        raise SungrowModbusError(msg)
                    last_error = SungrowModbusError(msg)
                    if attempt + 1 < _READ_ATTEMPTS and _is_connection_error(str(result)):
                        self._recreate_client()
                        continue
                    self._recreate_client()
                    raise last_error
                return result
            except SungrowModbusError:
                raise
            except (ConnectionException, ModbusException, OSError, TimeoutError) as err:
                last_error = SungrowModbusError(f"Modbus {kind} at {address} (count {count}) failed: {err}")
                if attempt + 1 < _READ_ATTEMPTS:
                    _LOGGER.debug(
                        "Modbus connection error on %s:%s (attempt %s); reconnecting: %s",
                        self.host,
                        self.port,
                        attempt + 1,
                        err,
                    )
                    self._recreate_client()
                    continue
                self._recreate_client()
                raise last_error from err
            except Exception as err:  # pylint: disable=broad-except
                self._recreate_client()
                raise SungrowModbusError(f"Modbus {kind} at {address} (count {count}) failed: {err}") from err
        assert last_error is not None
        raise last_error

    def close(self) -> None:
        """Close the underlying connection (entry unload / failed setup cleanup)."""
        self._recreate_client()


def _is_exception_code(message: str, code: int) -> bool:
    return f"exception_code={code}" in message


def _is_unsupported_address(err: SungrowModbusError) -> bool:
    """Return True when ``err`` is a Modbus 'Illegal Data Address' exception."""
    return _is_exception_code(str(err), 2)


def _is_connection_error(message: str) -> bool:
    """Return True when a failure looks like a dropped TCP session."""
    lower = message.lower()
    return any(
        token in lower
        for token in (
            "not connected",
            "connection",
            "broken pipe",
            "connection reset",
            "timed out",
            "timeout",
            "eof",
        )
    )
