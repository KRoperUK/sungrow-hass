"""Sungrow WiNet-S local Modbus register maps (#159).

Maps documented Sungrow Modbus registers to the same measure-point **codes** the
cloud transport emits, so entities are identical regardless of which source a value
comes from. Addresses here are the **on-the-wire** address, which is the documented
register number minus one (doc 5000 -> wire 4999). 32-bit values are low-word-first.

The SG-RS input-register set below was validated against a live SG3.6RS + WiNet-S.
Other models (SH hybrids, larger three-phase inverters) add their own maps, selected
at runtime by the device-type code in register 5000.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Modbus function codes.
FUNC_INPUT = 4  # read input registers (readings)
FUNC_HOLDING = 3  # read holding registers (config/control)


@dataclass(frozen=True)
class ModbusPoint:
    """One decoded value read from the inverter over Modbus.

    ``address`` is the on-the-wire register address (documented number - 1).
    ``code`` matches the cloud transport's measure-point code so both sources feed
    the same entity. ``data_type`` is one of ``u16``/``s16``/``u32``/``s32``;
    32-bit types consume two registers (low word first). The displayed value is the
    raw register value multiplied by ``scale``.
    """

    address: int
    code: str
    data_type: str
    scale: float
    unit: str | None = None

    @property
    def register_count(self) -> int:
        """Number of 16-bit registers this point occupies (1 or 2)."""
        return 2 if self.data_type in ("u32", "s32") else 1


# String-inverter (SG-RS) input registers — Modbus function 0x04. Codes are reused
# from the cloud/diagnostic point set so a value read locally lands on the same
# entity as its cloud counterpart.
SG_RS_INPUT_POINTS: tuple[ModbusPoint, ...] = (
    ModbusPoint(4999, "device_type_code", "u16", 1, None),
    ModbusPoint(5002, "daily_yield", "u16", 0.1, "kWh"),
    ModbusPoint(5003, "total_yield", "u32", 1, "kWh"),
    ModbusPoint(5007, "internal_temperature", "s16", 0.1, "°C"),
    ModbusPoint(5010, "mppt1_voltage", "u16", 0.1, "V"),
    ModbusPoint(5011, "mppt1_current", "u16", 0.1, "A"),
    ModbusPoint(5012, "mppt2_voltage", "u16", 0.1, "V"),
    ModbusPoint(5013, "mppt2_current", "u16", 0.1, "A"),
    ModbusPoint(5016, "total_dc_power", "u32", 1, "W"),
    ModbusPoint(5018, "phase_a_voltage", "u16", 0.1, "V"),
    ModbusPoint(5030, "total_active_power", "s32", 1, "W"),
    ModbusPoint(5035, "grid_frequency", "u16", 0.1, "Hz"),
)

# Register maps keyed by inverter family. Extended per-model as they're validated.
REGISTER_MAPS: dict[str, tuple[ModbusPoint, ...]] = {
    "sg_rs": SG_RS_INPUT_POINTS,
}


def decode_registers(
    points: tuple[ModbusPoint, ...], block_start: int, registers: list[int]
) -> dict[str, dict[str, Any]]:
    """Decode a contiguous input-register block into ``{code: {value, unit, ...}}``.

    ``registers`` is the raw 16-bit values read starting at ``block_start``. Each
    point is decoded from its offset within the block; a point that falls outside the
    block is skipped. The returned shape mirrors the cloud transport's per-point dict
    (``code``/``value``/``unit``) plus a ``source`` marker so the origin is accountable.
    """
    out: dict[str, dict[str, Any]] = {}
    for point in points:
        offset = point.address - block_start
        if offset < 0 or offset + point.register_count > len(registers):
            continue
        raw = _combine(registers, offset, point.data_type)
        out[point.code] = {
            "code": point.code,
            "value": round(raw * point.scale, 3),
            "unit": point.unit,
            "source": "modbus",
        }
    return out


def _combine(registers: list[int], offset: int, data_type: str) -> int:
    """Combine one/two registers into a signed/unsigned integer (32-bit low word first)."""
    if data_type in ("u32", "s32"):
        value = registers[offset] + (registers[offset + 1] << 16)
        bits = 32
    else:
        value = registers[offset]
        bits = 16
    if data_type in ("s16", "s32") and value >= (1 << (bits - 1)):
        value -= 1 << bits
    return value


def block_bounds(points: tuple[ModbusPoint, ...]) -> tuple[int, int]:
    """Return ``(start_address, count)`` covering every point in one contiguous read."""
    start = min(p.address for p in points)
    end = max(p.address + p.register_count for p in points)
    return start, end - start
