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
FUNC_WRITE_SINGLE = 6  # write single holding register


# ---------------------------------------------------------------------------
# #220 local control — holding maps (wire addresses = doc − 1)
# ---------------------------------------------------------------------------
# Validated live on SG3.6RS + WiNet-S (2026-07-18): wire 5006=0xAA enable,
# 5007=1000 (100%), FC6 no-op write+readback ok; hybrid EMS 13049 unsupported.
# Never write start/stop (wire 5005) or hybrid EMS without a separate SH map.


@dataclass(frozen=True)
class HoldingProbePoint:
    """One holding register considered for local control probing (#220)."""

    name: str
    wire_address: int
    description: str
    family: str  # "sg_rs" | "sh_rt" | "probe_negative"
    scale: float = 1.0
    unit: str | None = None
    # If True, the live probe may attempt a gated no-op / restore write.
    write_candidate: bool = False


@dataclass(frozen=True)
class HoldingControlPoint:
    """Production holding register mapped to a cloud dispatch parameter name (#220).

    Values written via ``async_update_parameters`` are already cloud-encoded strings
    (e.g. active power ratio 100% → ``"1000"``, enable → ``"170"``). The holding
    path stores that integer in a single U16 register.
    """

    param: str
    wire_address: int
    description: str
    # Cloud Appendix-10 param_code when known (for read-back id field).
    param_code: str | None = None


# Active power limit on string inverters (doc 5007 enable / 5008 ratio → wire −1).
SG_RS_ACTIVE_POWER_LIMIT_ENABLE = HoldingProbePoint(
    name="active_power_limit_enable",
    wire_address=5006,
    description="Active power limitation switch (0xAA enable / 0x55 disable)",
    family="sg_rs",
    write_candidate=False,
)
SG_RS_ACTIVE_POWER_LIMIT_RATIO = HoldingProbePoint(
    name="active_power_limit_ratio",
    wire_address=5007,
    description="Active power limit ratio (0.1 % units; 1000 = 100%)",
    family="sg_rs",
    scale=0.1,
    unit="%",
    write_candidate=True,
)

# Hybrid EMS block — expected illegal/unsupported on pure SG-RS (negative control).
SH_EMS_MODE = HoldingProbePoint(
    name="energy_management_mode",
    wire_address=13049,
    description="Hybrid EMS mode (doc 13050); SH-only negative probe on SG-RS",
    family="probe_negative",
    write_candidate=False,
)

# Absolute no-write list for the spike and production SG-RS control.
HOLDING_WRITE_DENYLIST_WIRE: frozenset[int] = frozenset(
    {
        5005,  # start/stop (0xCF/0xCE)
        13049,  # EMS mode
        13050,  # charge/discharge command
        13051,  # charge/discharge power
        13079,  # external EMS heartbeat
    }
)

SG_RS_HOLDING_PROBE_POINTS: tuple[HoldingProbePoint, ...] = (
    SG_RS_ACTIVE_POWER_LIMIT_ENABLE,
    SG_RS_ACTIVE_POWER_LIMIT_RATIO,
    SH_EMS_MODE,
)

# Cloud param names → holding wires for ModbusControl (family-keyed).
SG_RS_HOLDING_CONTROL_POINTS: tuple[HoldingControlPoint, ...] = (
    HoldingControlPoint(
        param="limited_power_switch",
        wire_address=5006,
        description="Active power limitation enable (0xAA/0x55 = 170/85)",
        param_code="10007",
    ),
    HoldingControlPoint(
        param="active_power_limit_ratio",
        wire_address=5007,
        description="Active power limit ratio (0.1 %; 1000 = 100%)",
        param_code="10008",
    ),
)

HOLDING_CONTROL_MAPS: dict[str, tuple[HoldingControlPoint, ...]] = {
    "sg_rs": SG_RS_HOLDING_CONTROL_POINTS,
    # Three-phase string maps share the low holding block on community maps.
    "sg_rt": SG_RS_HOLDING_CONTROL_POINTS,
}

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

    ``omit_zero`` drops a raw zero when the firmware reports 0 instead of the NAN
    sentinel for an absent channel (e.g. phase B/C voltage and MPPT3 on SG3.6RS).
    Do not set this on points where zero is a legitimate night-time reading with a
    live sibling channel (e.g. MPPT1 current at 0 A while voltage is non-zero).
    """

    address: int
    code: str
    data_type: str
    scale: float
    unit: str | None = None
    nan_value: int | None = None
    omit_zero: bool = False

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
    # MPPT3 present on some multi-tracker string inverters; omit when firmware
    # returns 0 / 0xFFFF on 2-tracker models such as SG3.6RS.
    ModbusPoint(5014, "mppt3_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5015, "mppt3_current", "u16", 0.1, "A", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5016, "total_dc_power", "u32", 1, "W"),
    ModbusPoint(5018, "phase_a_voltage", "u16", 0.1, "V"),
    # Single-phase inverters often report B/C as 0 rather than 0xFFFF.
    ModbusPoint(5019, "phase_b_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5020, "phase_c_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5030, "total_active_power", "s32", 1, "W"),
    ModbusPoint(5032, "reactive_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5034, "power_factor", "s16", 0.001, None, nan_value=NAN_S16),
    ModbusPoint(5035, "grid_frequency", "u16", 0.1, "Hz"),
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
    ModbusPoint(5014, "mppt3_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5015, "mppt3_current", "u16", 0.1, "A", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5016, "total_dc_power", "u32", 1, "W"),
    ModbusPoint(5018, "phase_a_voltage", "u16", 0.1, "V"),
    ModbusPoint(5019, "phase_b_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5020, "phase_c_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5030, "total_active_power", "s32", 1, "W"),
    ModbusPoint(5032, "reactive_power", "s32", 1, "W", nan_value=NAN_S32),
    ModbusPoint(5034, "power_factor", "s16", 0.001, None, nan_value=NAN_S16),
    ModbusPoint(5035, "grid_frequency", "u16", 0.1, "Hz"),
    ModbusPoint(5114, "mppt4_voltage", "u16", 0.1, "V", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5115, "mppt4_current", "u16", 0.1, "A", nan_value=NAN_U16, omit_zero=True),
    ModbusPoint(5213, "battery_power", "s32", 1, "W", nan_value=NAN_S32),
    # Preferred grid-frequency register on hybrids (mkaiser/SHx scale 0.01 Hz).
    ModbusPoint(5241, "grid_frequency", "u16", 0.01, "Hz"),
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
# SG three-phase string inverters share the low-block layout with SG-RS (phase B/C
# present when not NAN); hybrid SH-RS/RT share the expanded SH map (MIT mkaiser).
REGISTER_MAPS: dict[str, tuple[ModbusPoint, ...]] = {
    "sg_rs": SG_RS_INPUT_POINTS,
    "sg_rt": SG_RS_INPUT_POINTS,  # three-phase string — same low-block layout (#219)
    "sh_rt": SH_RT_INPUT_POINTS,
    "sh_rs": SH_RT_INPUT_POINTS,  # SH-RS shares the same input-register layout
}

# Map device-type codes reported in register 5000 to a register-map family.
# SH codes from the mkaiser SHx YAML (MIT) device-type map; SG-RS validated live.
# Unknown codes fall back to the configured model string / model_capabilities.
DEVICE_TYPE_CODE_TO_FAMILY: dict[int, str] = {
    # SG-RS string inverters (single-phase, e.g. SG3.6RS)
    9732: "sg_rs",  # 0x2604
    # SH single-phase hybrid (0x0Dxx) — mkaiser device-type map
    3331: "sh_rs",  # SH5K-V13 (0x0D03)
    3334: "sh_rs",  # SH3K6 (0x0D06)
    3335: "sh_rs",  # SH4K6 (0x0D07)
    3337: "sh_rs",  # SH5K-20 (0x0D09)
    3338: "sh_rs",  # SH3K6-30 (0x0D0A)
    3339: "sh_rs",  # SH4K6-30 (0x0D0B)
    3340: "sh_rs",  # SH5K-30 (0x0D0C)
    3341: "sh_rs",  # SH3.6RS (0x0D0D)
    3343: "sh_rs",  # SH5.0RS (0x0D0F)
    3344: "sh_rs",  # SH6.0RS (0x0D10)
    3351: "sh_rs",  # SH3.0RS (0x0D17)
    3352: "sh_rs",  # SH4.0RS (0x0D18)
    3354: "sh_rs",  # SH8.0RS (0x0D1A)
    3355: "sh_rs",  # SH10RS (0x0D1B)
    3367: "sh_rs",  # MG5RL (0x0D27)
    3368: "sh_rs",  # MG6RL (0x0D28)
    # SH three-phase hybrid (0x0Exx)
    3584: "sh_rt",  # SH5.0RT (0x0E00)
    3585: "sh_rt",  # SH6.0RT (0x0E01)
    3586: "sh_rt",  # SH8.0RT (0x0E02)
    3587: "sh_rt",  # SH10RT (0x0E03)
    3592: "sh_rt",  # SH5.0RT-V122 (0x0E08)
    3593: "sh_rt",  # SH6.0RT-V122 (0x0E09)
    3594: "sh_rt",  # SH8.0RT-V122 (0x0E0A)
    3595: "sh_rt",  # SH10RT-V122 (0x0E0B)
    3596: "sh_rt",  # SH5.0RT-V112 (0x0E0C)
    3597: "sh_rt",  # SH6.0RT-V112 (0x0E0D)
    3598: "sh_rt",  # SH8.0RT-V112 (0x0E0E)
    3599: "sh_rt",  # SH10RT-V112 (0x0E0F)
    3600: "sh_rt",  # SH5.0RT-20 (0x0E10)
    3601: "sh_rt",  # SH6.0RT-20 (0x0E11)
    3602: "sh_rt",  # SH8.0RT-20 (0x0E12)
    3603: "sh_rt",  # SH10RT-20 (0x0E13)
    3616: "sh_rt",  # SH5T (0x0E20)
    3617: "sh_rt",  # SH6T (0x0E21)
    3618: "sh_rt",  # SH8T (0x0E22)
    3619: "sh_rt",  # SH10T (0x0E23)
    3620: "sh_rt",  # SH12T (0x0E24)
    3621: "sh_rt",  # SH15T (0x0E25)
    3622: "sh_rt",  # SH20T (0x0E26)
    3624: "sh_rt",  # SH25T (0x0E28)
}


def family_for_device_type_code(device_type_code: int | None) -> str | None:
    """Return the register-map family for a device-type code, if known."""
    if device_type_code is None:
        return None
    return DEVICE_TYPE_CODE_TO_FAMILY.get(device_type_code)


# ---------------------------------------------------------------------------
# Enum decoders for local Modbus values (#322)
# ---------------------------------------------------------------------------
# The register maps expose three raw integer registers that are only useful as
# human-readable state names / model names / power-flow flags. Rather than
# hard-code the decoding inside each entity class, define the lookup tables here
# and register them into ``ENUM_MAPS`` in ``measure_points_data`` so the existing
# enum sensor pipeline (options, translation, resolve_enum_value) handles the
# rest uniformly for cloud and Modbus points.

# Inverter running state (wire register 12999).
# Source: mkaiser SHx YAML ``sg_inverter_state`` template map (MIT).
# Some raw codes deliberately share a display name (e.g. 0x0000 and 0x0040 both
# "Running") — that reflects Sungrow's own mapping and is preserved verbatim.
RUNNING_STATE_NAMES: dict[int, str] = {
    0x0000: "Running",
    0x0001: "Stop",
    0x0002: "Key stop",
    0x0004: "Emergency Stop",
    0x0008: "Standby",
    0x0010: "Initial standby",
    0x0014: "Microgrid Operation",
    0x0020: "Starting",
    0x0040: "Running",
    0x0041: "Off-grid Charge",
    0x0080: "Derating Running",
    0x0100: "Fault",
    0x0200: "Update Failed",
    0x0400: "Running in maintain mode",
    0x0800: "Running in compulsory (forced) mode",
    0x1000: "Running (off-grid)",
    0x1111: "Uninitialized",
    0x1200: "Initial standby",
    0x1300: "Key stop",
    0x1400: "Standby",
    0x1500: "Emergency Stop",
    0x1600: "Starting",
    0x1700: "AFCI self-test shutdown",
    0x1800: "Intelligent Station Building Status",
    0x1900: "Safe Mode",
    0x2000: "Open loop",
    0x2500: "Communicate fault",
    0x2501: "Restarting",
    0x4000: "Running in External EMS mode",
    0x4001: "Emergency Charging Operation",
    0x5500: "Fault",
    0x8000: "Stop",
    0x8100: "Derating Running",
    0x8200: "Dispatch Running",
    0x9100: "Warn Running",
}

# Device model name (wire register 4999, ``device_type_code``).
# SH codes from the mkaiser SHx YAML ``sg_device_type`` template map (MIT).
# The SG string code 9732 (0x2604) is shared across the SG-RS single-phase family
# (SG3.0RS / SG3.6RS / SG5.0RS all report the same code in field reports); use
# the generic family name so the sensor's option list doesn't imply a specific
# wattage that can't be verified from Modbus alone.
DEVICE_MODEL_NAMES: dict[int, str] = {
    # SG string inverters — Sungrow does not distinguish specific SG-RS variants
    # in this register, so the option is family-generic.
    9732: "SG-RS string inverter",  # 0x2604
    # SH single-phase hybrid (0x0Dxx)
    3331: "SH5K-V13",
    3334: "SH3K6",
    3335: "SH4K6",
    3337: "SH5K-20",
    3338: "SH3K6-30",
    3339: "SH4K6-30",
    3340: "SH5K-30",
    3341: "SH3.6RS",
    3343: "SH5.0RS",
    3344: "SH6.0RS",
    3351: "SH3.0RS",
    3352: "SH4.0RS",
    3354: "SH8.0RS",
    3355: "SH10RS",
    3367: "MG5RL",
    3368: "MG6RL",
    # SH three-phase hybrid (0x0Exx)
    3584: "SH5.0RT",
    3585: "SH6.0RT",
    3586: "SH8.0RT",
    3587: "SH10RT",
    3592: "SH5.0RT-V122",
    3593: "SH6.0RT-V122",
    3594: "SH8.0RT-V122",
    3595: "SH10RT-V122",
    3596: "SH5.0RT-V112",
    3597: "SH6.0RT-V112",
    3598: "SH8.0RT-V112",
    3599: "SH10RT-V112",
    3600: "SH5.0RT-20",
    3601: "SH6.0RT-20",
    3602: "SH8.0RT-20",
    3603: "SH10RT-20",
    3616: "SH5T",
    3617: "SH6T",
    3618: "SH8T",
    3619: "SH10T",
    3620: "SH12T",
    3621: "SH15T",
    3622: "SH20T",
    3624: "SH25T",
}

# Modbus point-code -> enum table, consumed by ``measure_points_data.ENUM_MAPS``.
# Each entry turns a raw integer register value into a documented display string
# (see :func:`resolve_enum_value`). Keys are the local Modbus point codes as
# emitted by :func:`decode_registers`, so the enum pipeline picks them up
# automatically for any Modbus point whose value hits one of these codes.
MODBUS_ENUM_MAPS: dict[str, dict[int, str]] = {
    "running_state_raw": RUNNING_STATE_NAMES,
    "device_type_code": DEVICE_MODEL_NAMES,
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
        if point.nan_value is not None and raw == point.nan_value:
            continue
        if point.omit_zero and raw == 0:
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


# Modbus function 4 (read input registers) is capped by the protocol at 125
# registers per request. pymodbus enforces this client-side and the WiNet-S
# dongle is happier with even smaller reads; the default keeps some headroom.
# See issue #318 — the SH-RT/SH-RS map collapsed 4999..5241 into a single
# 243-register block and pymodbus rejected the request with
# ``1 < count 243 < 125 !`` before it ever hit the wire.
MODBUS_MAX_READ_COUNT = 125
DEFAULT_MAX_BLOCK_SIZE = 100


def block_partitions(
    points: tuple[ModbusPoint, ...],
    max_gap: int = 256,
    max_block_size: int = DEFAULT_MAX_BLOCK_SIZE,
) -> list[tuple[int, int]]:
    """Split ``points`` into contiguous read blocks small enough for one Modbus read.

    Modbus limits the number of registers per read (spec max = 125, and WiNet-S
    dongles are happier with less). This partitions the map so each block:

    * is separated from the next by more than ``max_gap`` registers, and
    * never exceeds ``max_block_size`` registers (defaults to
      :data:`DEFAULT_MAX_BLOCK_SIZE`, leaving headroom under the 125 hard cap).

    Reading one huge block from 4999 to 13045 would fail; this yields a handful
    of small reads instead. ``max_block_size`` is clamped to
    :data:`MODBUS_MAX_READ_COUNT` because pymodbus rejects anything larger.
    """
    if not points:
        return []
    cap = min(max_block_size, MODBUS_MAX_READ_COUNT)
    sorted_points = sorted(points, key=lambda p: p.address)
    blocks: list[tuple[int, int]] = []
    block_start = sorted_points[0].address
    block_end = sorted_points[0].address + sorted_points[0].register_count
    for point in sorted_points[1:]:
        point_end = point.address + point.register_count
        would_exceed_cap = (point_end - block_start) > cap
        gap_too_wide = point.address > block_end + max_gap
        if gap_too_wide or would_exceed_cap:
            blocks.append((block_start, block_end - block_start))
            block_start = point.address
            block_end = point_end
        else:
            block_end = max(block_end, point_end)
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
