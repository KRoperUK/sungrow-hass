"""Static measure-point data transcribed from the iSolarCloud docs.

Source: mcp-isolarcloud docs server (``common-*-measuring-points`` pages).
No logic here — see ``measure_points.py``.
"""

from __future__ import annotations

# --- Enum value tables (point_id -> {int_code: label}) ------------------------

_CHARGER_STATUS: dict[int, str] = {
    1: "Idle (not plugged in)",
    2: "Standby (plugged in)",
    3: "Charging",
    4: "Charging paused (station)",
    5: "Charging paused (vehicle)",
    6: "Charging completed",
    7: "Reserved",
    8: "Disabled",
    9: "Fault",
}

# Inverter operating status (point 29) and energy-storage-inverter operating
# status (point 13146) share this documented family.
_OPERATING_STATUS: dict[int, str] = {
    0: "Grid-connected operation",
    20: "Microgrid operation",
    64: "Grid-connected, operating normally",
    65: "Off-grid charging",
    128: "Derated running",
    256: "Operation fault",
    512: "Update failed",
    1024: "Maintenance mode",
    2048: "Forced mode",
    4096: "Off-grid",
    4369: "Uninitialized",
    4608: "Initial standby",
    4864: "Awaiting startup",
    5120: "Standby",
    5376: "Emergency stop",
    5632: "Starting up",
    5888: "AFCI self-test",
    6144: "Smart plant building status",
    6400: "Security mode",
    8192: "Open loop",
    9472: "LCD/DSP communication fault",
    9473: "Restarting",
    16384: "Energy dispatch mode",
    16385: "Emergency charging mode",
    16435: "Low insulation resistance",
    16439: "Insulation board anomaly",
    20992: "Shutting down",
    21760: "Shut down due to faults",
    32768: "Shut down",
    33024: "Derated running",
    33280: "Dispatched running",
    33792: "Running under anti-PID condition",
    37120: "Running with alarm",
}

_MICROINVERTER_STATUS: dict[int, str] = {
    0: "On-grid operation",
    1024: "Maintenance mode",
    2048: "Forced mode",
    4096: "Backup operation",
    4369: "Initial status",
    4608: "Initial standby",
    4864: "Press to shut down",
    5120: "Standby",
    5632: "Starting up",
    8192: "Open loop",
    21760: "Shut down due to faults",
    32768: "Shutdown",
    33024: "Derating running",
    33280: "Dispatch running",
    37120: "Warn run",
}

ENUM_MAPS: dict[str, dict[int, str]] = {
    "33716": _CHARGER_STATUS,
    "29": _OPERATING_STATUS,
    "13146": _OPERATING_STATUS,
    "51301": _MICROINVERTER_STATUS,
}

# RAW_POINTS and CODE_ALIASES are populated in later tasks.
RAW_POINTS: list[tuple[str, str, str]] = []
CODE_ALIASES: dict[str, str] = {}
