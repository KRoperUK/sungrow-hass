"""Sungrow WiNet-S local Modbus register maps (#159).

Maps documented Sungrow Modbus registers to the same measure-point **codes** the
cloud transport emits, so entities are identical regardless of which source a value
comes from. Addresses here are the **on-the-wire** address, which is the documented
register number minus one (doc 5000 -> wire 4999). 32-bit values are low-word-first.

The SG-RS input-register set was validated against a live SG3.6RS + WiNet-S.
Additional maps and register definitions for the SH-RT / SH-RS hybrid families are
derived from the community-maintained YAML Modbus integration by mkaiser at
https://github.com/mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant — used under
the terms of its MIT license.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Modbus function codes.
FUNC_INPUT = 4  # read input registers (readings)
FUNC_HOLDING = 3  # read holding registers (config/control)

# Sentinel values a register can return when the point is not supported by the
# attached hardware/firmware.
NAN_U16 = 0xFFFF
NAN_S16 = 0x7FFF
NAN_S32 = 0x7FFFFFFF


@dataclass(frozen=True)
class ModbusPoint:
    """One decoded value read from the inverter over Modbus.

    ``address`` is the on-the-wire register address (documented number - 1).
    ``code`` matches the cloud transport's measure-point code so both sources feed
    the same entity. ``data_type`` is one of ``u16``/``s16``/``u32``/``s32``;
    32-bit types consume two registers (low word first). The displayed value is the
    raw register value multiplied by ``scale``.

    ``nan_value`` is the raw sentinel that means "not available on this hardware"
    (e.g. 0xFFFF for an MPPT that the inverter does not have). When the raw value
    equals this sentinel the point is omitted, so unsupported registers do not
    surface as bogus sensors.
    """

    address: int
    code: str
    data_type: str
    scale: float
    unit: str | None = None
    nan_value: int | None = None

    @property
    def register_count(self) -> int:
        """Number of 16-bit registers this point occupies (1 or 2)."""
        return 2 if self.data_type in ("u32", "s32") else 1


# String-inverter (SG-RS) input registers — Modbus function 0x04. Codes are reused
# from the cloud/diagnostic point set so a value read locally lands on the same
# entity as its cloud counterpart.
# Register definitions validated against a live SG3.6RS; additional common
# single/three-phase string points from the mkaiser SHx mapping (see module doc).
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
    ModbusPoint(5019, "phase_b_voltage", "u16", 0.1, "V", nan_value=NAN_U16),
    ModbusPoint(5020, "phase_c_voltage", "u16", 0.1, "V", nan_value=NAN_U16),
    ModbusPoint(5030, "total_active_power", "s32", 1, "W"),
    ModbusPoint(5032, "reactive_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5034, "power_factor", "s16", 0.001, None, nan_value=NAN_S16),
    ModbusPoint(5035, "grid_frequency", "u16", 0.1, "Hz"),
    ModbusPoint(13035, "daily_imported_energy", "u16", 0.1, "kWh", nan_value=NAN_U16),
    ModbusPoint(13036, "total_imported_energy", "u32", 0.1, "kWh", nan_value=NAN_U16),
    ModbusPoint(13044, "daily_exported_energy", "u16", 0.1, "kWh", nan_value=NAN_U16),
    ModbusPoint(13045, "total_exported_energy", "u32", 0.1, "kWh", nan_value=NAN_U16),
)

# Hybrid inverter (SH-RT / SH-RS) input registers — Modbus function 0x04.
# Derived from the mkaiser SHx YAML mapping (MIT license) with on-the-wire
# addresses and low-word-first 32-bit decoding.
SH_RT_INPUT_POINTS: tuple[ModbusPoint, ...] = (
    ModbusPoint(4999, "device_type_code", "u16", 1, None),
    ModbusPoint(5002, "daily_pv_gen_battery_discharge", "u16", 0.1, "kWh"),
    ModbusPoint(5003, "total_pv_gen_battery_discharge", "u32", 0.1, "kWh"),
    ModbusPoint(5007, "internal_temperature", "s16", 0.1, "°C"),
    ModbusPoint(5010, "mppt1_voltage", "u16", 0.1, "V"),
    ModbusPoint(5011, "mppt1_current", "u16", 0.1, "A"),
    ModbusPoint(5012, "mppt2_voltage", "u16", 0.1, "V"),
    ModbusPoint(5013, "mppt2_current", "u16", 0.1, "A"),
    ModbusPoint(5014, "mppt3_voltage", "u16", 0.1, "V", nan_value=NAN_U16),
    ModbusPoint(5015, "mppt3_current", "u16", 0.1, "A", nan_value=NAN_U16),
    ModbusPoint(5016, "total_dc_power", "u32", 1, "W"),
    ModbusPoint(5018, "phase_a_voltage", "u16", 0.1, "V"),
    ModbusPoint(5019, "phase_b_voltage", "u16", 0.1, "V", nan_value=NAN_U16),
    ModbusPoint(5020, "phase_c_voltage", "u16", 0.1, "V", nan_value=NAN_U16),
    ModbusPoint(5030, "total_active_power", "s32", 1, "W"),
    ModbusPoint(5032, "reactive_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5034, "power_factor", "s16", 0.001, None, nan_value=NAN_S16),
    ModbusPoint(5035, "grid_frequency", "u16", 0.1, "Hz"),
    ModbusPoint(5114, "mppt4_voltage", "u16", 0.1, "V", nan_value=NAN_U16),
    ModbusPoint(5115, "mppt4_current", "u16", 0.1, "A", nan_value=NAN_U16),
    ModbusPoint(5213, "battery_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5241, "grid_frequency", "u16", 0.1, "Hz"),
    ModbusPoint(5600, "meter_active_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5602, "meter_phase_a_active_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5604, "meter_phase_b_active_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5606, "meter_phase_c_active_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5621, "export_power_limit_min", "u16", 10, "W", nan_value=NAN_U16),
    ModbusPoint(5622, "export_power_limit_max", "u16", 10, "W", nan_value=NAN_U16),
    ModbusPoint(5627, "bdc_rated_power", "u16", 100, "W", nan_value=NAN_U16),
    ModbusPoint(5630, "battery_current", "s16", 0.1, "A", nan_value=NAN_S16),
    ModbusPoint(5634, "bms_max_charging_current", "u16", 1, "A", nan_value=NAN_U16),
    ModbusPoint(5635, "bms_max_discharging_current", "u16", 1, "A", nan_value=NAN_U16),
    ModbusPoint(5638, "battery_capacity", "u16", 0.01, "kWh", nan_value=NAN_U16),
    ModbusPoint(5722, "backup_phase_a_power", "s16", 1, "W", nan_value=NAN_S16),
    ModbusPoint(5723, "backup_phase_b_power", "s16", 1, "W", nan_value=NAN_S16),
    ModbusPoint(5724, "backup_phase_c_power", "s16", 1, "W", nan_value=NAN_S16),
    ModbusPoint(5725, "total_backup_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5740, "meter_phase_a_voltage", "s16", 0.1, "V", nan_value=NAN_S16),
    ModbusPoint(5741, "meter_phase_b_voltage", "s16", 0.1, "V", nan_value=NAN_S16),
    ModbusPoint(5742, "meter_phase_c_voltage", "s16", 0.1, "V", nan_value=NAN_S16),
    ModbusPoint(5743, "meter_phase_a_current", "u16", 0.01, "A", nan_value=NAN_U16),
    ModbusPoint(5744, "meter_phase_b_current", "u16", 0.01, "A", nan_value=NAN_U16),
    ModbusPoint(5745, "meter_phase_c_current", "u16", 0.01, "A", nan_value=NAN_U16),
    ModbusPoint(12999, "running_state_raw", "u16", 1, None),
    ModbusPoint(13000, "power_flow_status", "u16", 1, None),
    ModbusPoint(13001, "daily_pv_generation", "u16", 0.1, "kWh"),
    ModbusPoint(13002, "total_pv_generation", "u32", 0.1, "kWh"),
    ModbusPoint(13004, "daily_exported_energy_from_pv", "u16", 0.1, "kWh"),
    ModbusPoint(13005, "total_exported_energy_from_pv", "u32", 0.1, "kWh"),
    ModbusPoint(13007, "load_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(13009, "export_power_raw", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(13011, "daily_battery_charge_from_pv", "u16", 0.1, "kWh"),
    ModbusPoint(13012, "total_battery_charge_from_pv", "u32", 0.1, "kWh"),
    ModbusPoint(13016, "daily_direct_energy_consumption", "u16", 0.1, "kWh"),
    ModbusPoint(13017, "total_direct_energy_consumption", "u32", 0.1, "kWh"),
    ModbusPoint(13019, "battery_voltage", "u16", 0.1, "V"),
    ModbusPoint(13022, "battery_level", "u16", 0.1, "%"),
    ModbusPoint(13023, "battery_state_of_health", "u16", 0.1, "%"),
    ModbusPoint(13024, "battery_temperature", "s16", 0.1, "°C"),
    ModbusPoint(13025, "daily_battery_discharge", "u16", 0.1, "kWh"),
    ModbusPoint(13026, "total_battery_discharge", "u32", 0.1, "kWh"),
    ModbusPoint(13030, "phase_a_current", "s16", 0.1, "A", nan_value=NAN_S16),
    ModbusPoint(13031, "phase_b_current", "s16", 0.1, "A", nan_value=NAN_S16),
    ModbusPoint(13032, "phase_c_current", "s16", 0.1, "A", nan_value=NAN_S16),
    ModbusPoint(13033, "total_active_power", "s32", 1, "W"),
    ModbusPoint(13035, "daily_imported_energy", "u16", 0.1, "kWh", nan_value=NAN_U16),
    ModbusPoint(13036, "total_imported_energy", "u32", 0.1, "kWh", nan_value=NAN_U16),
    ModbusPoint(13039, "daily_battery_charge", "u16", 0.1, "kWh"),
    ModbusPoint(13040, "total_battery_charge", "u32", 0.1, "kWh"),
    ModbusPoint(13044, "daily_exported_energy", "u16", 0.1, "kWh", nan_value=NAN_U16),
    ModbusPoint(13045, "total_exported_energy", "u32", 0.1, "kWh", nan_value=NAN_U16),
)

# Register maps keyed by inverter family.
REGISTER_MAPS: dict[str, tuple[ModbusPoint, ...]] = {
    "sg_rs": SG_RS_INPUT_POINTS,
    "sh_rt": SH_RT_INPUT_POINTS,
    "sh_rs": SH_RT_INPUT_POINTS,  # SH-RS shares the same input-register layout
}

# Map device-type codes reported in register 5000 to a register-map family.
# Derived from Sungrow docs and cross-referenced with the mkaiser SHx mapping.
# Unknown codes fall back to the configured model string.
DEVICE_TYPE_CODE_TO_FAMILY: dict[int, str] = {
    # SG-RS string inverters (single-phase, e.g. SG3.6RS)
    9732: "sg_rs",
}


def family_for_device_type_code(device_type_code: int | None) -> str | None:
    """Return the register-map family for a device-type code, if known."""
    if device_type_code is None:
        return None
    return DEVICE_TYPE_CODE_TO_FAMILY.get(device_type_code)


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
        if point.nan_value is not None and raw == point.nan_value:
            continue
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


def block_partitions(points: tuple[ModbusPoint, ...], max_gap: int = 256) -> list[tuple[int, int]]:
    """Split ``points`` into contiguous read blocks separated by gaps > ``max_gap``.

    Modbus limits the number of registers per read; reading one huge block from
    4999 to 13045 would fail. This partitions the map into nearby groups so each
    read stays small and efficient.
    """
    if not points:
        return []
    sorted_points = sorted(points, key=lambda p: p.address)
    blocks: list[tuple[int, int]] = []
    block_start = sorted_points[0].address
    block_end = sorted_points[0].address + sorted_points[0].register_count
    for point in sorted_points[1:]:
        if point.address > block_end + max_gap:
            blocks.append((block_start, block_end - block_start))
            block_start = point.address
            block_end = point.address + point.register_count
        else:
            block_end = max(block_end, point.address + point.register_count)
    blocks.append((block_start, block_end - block_start))
    return blocks


# ---------------------------------------------------------------------------
# #223 diagnostic dump
# ---------------------------------------------------------------------------
# Issue #223 reports the locally-read ``daily_yield`` diverging from the cloud value on
# the SG-RS. The current mapping (``reg 5002 wire, u16, * 0.1 kWh``) is correct against
# the documented "Daily power yields" register, so a single reading is not enough to
# tell whether the divergence is a constant scaling factor, an off-by-one register, or a
# firmware-semantics difference. To make the next daytime re-observation conclusive the
# integration captures the raw 16-bit values from a small register window around the
# candidate daily_yield positions and lists the values each hypothesis would produce, so
# the right mapping can be picked without guessing.

# Wire-register window: device_type_code (4999) .. mppt1_voltage (5010) inclusive.
# This covers the doc registers 5000..5011 — the area around "Daily power yields"
# (doc 5003 = wire 5002) and the 32-bit "Total power yields" (doc 5004-5005 = wire
# 5003-5004), plus a couple of addresses either side to test off-by-one.
DAILY_YIELD_DIAG_START = 4999
DAILY_YIELD_DIAG_COUNT = 12  # 4999..5010

# Candidate wire addresses to interpret as the "today" total. The current mapping
# (``5002``) is the documented "Daily power yields"; ``5000``/``5001`` test an
# off-by-one; ``5004``/``5005`` test if the firmware moved the daily total into
# the area the doc reserves for the 32-bit lifetime total.
DAILY_YIELD_DIAG_CANDIDATE_ADDRESSES: tuple[int, ...] = (5000, 5001, 5002, 5003, 5004, 5005)

# Candidate scaling factors. The doc says 0.1 kWh per LSB, but firmware revisions
# have used 0.01 kWh and 1 Wh on other models; we surface all three so a daytime
# capture picks the one matching the entity's reported value.
DAILY_YIELD_DIAG_SCALES: tuple[tuple[float, str], ...] = (
    (0.1, "kWh"),
    (0.01, "kWh"),
    (1.0, "Wh"),
    (10.0, "—"),
)


def daily_yield_diagnostic_dump(registers: list[int], block_start: int = DAILY_YIELD_DIAG_START) -> dict[str, Any]:
    """Build a structured diagnostic for #223 from a raw register block.

    ``registers`` is the raw 16-bit values read starting at ``block_start``
    (default :data:`DAILY_YIELD_DIAG_START`, so the list aligns directly with the
    wire-address numbers). A block that doesn't cover the full diagnostic window is
    accepted and only the addresses present are reported; this lets the function be
    called with whatever the client has already read so the dump is free on a
    full-block read.

    The returned dict is the value surfaced on the daily_yield sensor's
    ``daily_yield_diagnostic`` attribute, and is deliberately compact + JSON-safe so
    it round-trips through the recorder.
    """
    raw: dict[str, int] = {}
    for offset, value in enumerate(registers):
        raw[str(block_start + offset)] = int(value)
    candidates: list[dict[str, Any]] = []
    for address in DAILY_YIELD_DIAG_CANDIDATE_ADDRESSES:
        offset = address - block_start
        if offset < 0 or offset >= len(registers):
            continue
        raw_value = int(registers[offset])
        for scale, unit in DAILY_YIELD_DIAG_SCALES:
            candidates.append(
                {
                    "address": address,
                    "raw": raw_value,
                    "scale": scale,
                    "unit": unit,
                    "value": round(raw_value * scale, 3),
                }
            )
    return {
        "start": block_start,
        "raw": raw,
        "candidates": candidates,
        "current_mapping": {
            "address": 5002,
            "raw": int(registers[5002 - block_start]) if 5002 - block_start in range(len(registers)) else None,
            "scale": 0.1,
            "unit": "kWh",
        },
    }
