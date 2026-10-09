"""The iSolarCloud measure-point catalog, sourced from the pysolarcloud library.

The documented measuring points (``(point_id, english_name, unit)`` rows) and the
documented value-enum tables are no longer transcribed here: they live in
``pysolarcloud``'s packaged catalog (``pysolarcloud.load_measure_points()``, shipped as
``measure_points.json``), the single source of truth shared by the library and this
integration (sungrow-hass#484, step 3 of #458). This module adapts that catalog into the
shapes the integration's ``measure_points`` resolver already consumes — ``RAW_POINTS``,
``ENUM_MAPS``, ``CODE_ALIASES`` — and layers on the integration-local additions the
library deliberately does not carry:

* **Point 58649** (SBR battery cell-balancing status, #501) — reverse-engineered, not in
  the published measuring-point docs, so it has no row in the library catalog.
* **``MODBUS_ENUM_MAPS``** — local-Modbus register enum tables (``modbus_registers``),
  which stay HA-local; the enum layer is therefore *split* (library cloud enums ∪ local
  Modbus enums), not lifted wholesale.
* **``CODE_ALIASES``** — Home Assistant display names, which are presentation and belong
  to the consumer, not the vendor-fact catalog.

No logic here — see ``measure_points.py``.
"""

from __future__ import annotations

from pysolarcloud import load_measure_points

from .modbus_registers import MODBUS_ENUM_MAPS

_CATALOG = load_measure_points()

# --- Enum value tables (point_id -> {int_code: label}) ------------------------

# SBR battery BMS cell-balancing status (point 58649, #501). Reverse-engineered:
# the point is NOT in the published measuring-point catalog — it was found by
# enumerating getDeviceRealTimeData IDs with is_get_point_dict=1 (getOpenPointInfo
# returns E900 for a developer app), and the raw name is 均衡状态 ("balancing status").
# 0=idle and 2=balancing are VERIFIED against the iSolarCloud portal
# (Maintenance → Curve → Device comparison) over several days on an SH6.0RT + SBR.
# Code 1 has never been observed and its meaning is UNVERIFIED, so it is deliberately
# left out of the table: resolve_enum_value maps any unlisted code to "unknown"
# rather than inventing a label (#113).
_BATTERY_BALANCING_STATUS: dict[int, str] = {
    0: "Idle",
    2: "Balancing",
}

# The documented cloud enum tables, keyed by the point ID that references them, come
# from the library catalog (``charger_status`` -> 33716, ``operating_status`` -> 29 &
# 13146, ``microinverter_status`` -> 51301). Built by walking the catalog's points so a
# new documented enum point in a future library release is picked up without a change
# here. ``decode_enum`` on the library takes int/str; this integration's resolver works
# on ``{int: label}`` tables, so materialise each referenced table into that shape.
_CLOUD_ENUM_MAPS: dict[str, dict[int, str]] = {
    point.point_id: dict(_CATALOG.enums[point.enum]) for point in _CATALOG.points.values() if point.enum is not None
}

ENUM_MAPS: dict[str, dict[int, str]] = {
    **_CLOUD_ENUM_MAPS,
    # Reverse-engineered local addition the published catalog does not carry (#501).
    "58649": _BATTERY_BALANCING_STATUS,
    # Local-Modbus point-code enum tables (running_state_raw, device_type_code)
    # merged in from ``modbus_registers`` so the existing enum sensor pipeline
    # (options, resolve_enum_value) handles cloud and Modbus points uniformly (#322).
    **MODBUS_ENUM_MAPS,
}

# --- Catalog rows: (point_id, english_name, unit). Blank unit = "". -----------
# The documented rows come from the library catalog, in document order; the library
# stores a missing unit as ``None`` where this integration has always used ``""``, so
# normalise it. Point 58649 (balancing status, #501) is appended because it is not in
# the published catalog (see the module docstring).
RAW_POINTS: list[tuple[str, str, str]] = [
    (point.point_id, point.name, point.unit or "") for point in _CATALOG.points.values()
] + [
    ("58649", "Balancing Status", ""),
]

# --- Friendly names for known codes (built-in default + recommended user codes) ---
CODE_ALIASES: dict[str, str] = {
    # Built-in plant/battery default codes (from pysolarcloud).
    "total_field_energy_storage_active_power": "Battery Power",
    "total_field_energy_storage_maximum_reactive_power": "Battery Max Reactive Power",
    "total_field_chargeable_energy": "Battery Chargeable Energy",
    "total_field_dischargeable_energy": "Battery Dischargeable Energy",
    "total_field_maximum_rechargeable_power": "Battery Max Charge Power",
    "total_field_maximum_dischargeable_power": "Battery Max Discharge Power",
    "total_field_power_factor": "Battery Power Factor",
    "total_field_reactive_power": "Battery Reactive Power",
    "total_field_soc": "Battery State of Charge (Field)",
    "daily_field_charge_capacity": "Battery Daily Charge Capacity",
    "daily_field_discharge_capacity": "Battery Daily Discharge Capacity",
    "total_field_charge_capacity": "Battery Total Charge Capacity",
    "total_field_discharge_capacity": "Battery Total Discharge Capacity",
    "total_number_of_charge_discharge": "Battery Charge/Discharge Cycles",
    "energy_storage_active_power_ems": "EMS Battery Power",
    "energy_storage_soc_ems": "EMS Battery SOC",
    "battery_level_soc": "Battery State of Charge",
    "meter_pr": "Meter Performance Ratio",
    "plant_pr": "Plant Performance Ratio",
    "inverter_pr": "Inverter Performance Ratio",
    "power_fraction": "Plant Power / Installed Power",
    # Recommended user-supplied codes (docs/SENSORS.md).
    "battery_charge_power": "Battery Charge Power",
    "battery_discharge_power": "Battery Discharge Power",
    "ev_charger_power": "EV Charger Power",
    "ev_charger_energy": "EV Charger Energy",
    "battery_level": "Battery Level",
    "battery_soh": "Battery Health (SOH)",
    "battery_voltage": "Battery Voltage",
    "battery_current": "Battery Current",
    "battery_temperature": "Battery Temperature",
    "battery_total_charge_energy": "Battery Total Charge Energy",
    "battery_total_discharge_energy": "Battery Total Discharge Energy",
    "ev_charger_max_power": "EV Charger Max Power",
    "ev_charger_status": "EV Charger Status",
    "meter_forward_active_energy": "Meter Forward Active Energy",
    "meter_reverse_active_energy": "Meter Reverse Active Energy",
    "meter_daily_forward_active_energy": "Meter Daily Forward Active Energy",
    "meter_daily_reverse_active_energy": "Meter Daily Reverse Active Energy",
    "meter_active_power": "Meter Active Power",
    "meter_power_factor": "Meter Power Factor",
    "meter_apparent_power": "Meter Apparent Power",
    "meter_frequency": "Meter Frequency",
    # Energy-storage-inverter recommended codes (docs/SENSORS.md).
    "battery_soc": "Battery Level (SOC)",
    "load_power": "Load Power",
    "feed_in_power": "Feed-in Power",
    "purchased_power": "Purchased Power",
    "inverter_operating_status": "Inverter Operating Status",
    # EMS recommended codes.
    "ems_storage_power": "EMS Storage Power",
    "ems_storage_soc": "EMS Storage SOC",
    "ems_grid_power": "EMS Grid Power",
    "ems_pv_power": "EMS PV Power",
    "ems_active_load": "EMS Active Load",
    "ems_total_charge": "EMS Total Charge",
    "ems_total_discharge": "EMS Total Discharge",
    # Microinverter recommended codes.
    "micro_active_power": "Microinverter Active Power",
    "micro_total_yield": "Microinverter Total Yield",
    "micro_yield_today": "Microinverter Yield Today",
    "micro_power_factor": "Microinverter Power Factor",
    "micro_running_status": "Microinverter Running Status",
    # Local Modbus enum sensors (#322): raw register-code sensors are decoded
    # to display strings via ENUM_MAPS. These aliases give them clear names
    # instead of the fallback title-cased "Running State Raw" / "Device Type Code".
    "running_state_raw": "Inverter State",
    "device_type_code": "Device Model",
    # Local Modbus identity strings (#323): pretty labels for the ASCII fields
    # the inverter/comm-module/battery expose over Modbus.
    "inverter_serial": "Serial Number",
    "inverter_firmware_version": "Firmware Version",
    "communication_module_firmware_version": "Communication Module Firmware",
    "battery_firmware_version": "Battery Firmware Version",
    # ARM + DSP subsystem software versions (#333). Populated on models
    # that carry certification strings for their internal controllers.
    "arm_software_version": "ARM Software Version",
    "dsp_software_version": "DSP Software Version",
    # Sungrow Modbus protocol version (BCD-packed u32 at wire 4951; #333).
    "protocol_version": "Modbus Protocol Version",
    # Per-device diagnostic codes whose acronyms/initialisms the generic
    # title-caser would mangle ("Mppt1", "Wlan", "Afci", "Dc", "Id").
    "mppt1_voltage": "MPPT1 Voltage",
    "mppt1_current": "MPPT1 Current",
    "mppt2_voltage": "MPPT2 Voltage",
    "mppt2_current": "MPPT2 Current",
    "mppt3_voltage": "MPPT3 Voltage",
    "mppt3_current": "MPPT3 Current",
    "total_dc_power": "Total DC Power",
    "negative_voltage_to_ground": "Negative Voltage to Ground",
    "afci_fault_count": "AFCI Fault Count",
    "wlan_signal_strength": "WLAN Signal Strength",
    "battery_dc_contactor_status": "Battery DC Contactor Status",
    "battery_fault_module_id": "Battery Fault Module ID",
    "battery_balancing_status": "Battery Balancing Status",
    # Named fields from getPsDetail (cloud_user transport, #292).
    "current_power": "Current Power",
    "today_energy": "Energy Today",
    "total_energy": "Total Energy",
    "month_energy": "Energy This Month",
    "co2_reduce_total": "CO₂ Reduction Total",
    "today_income": "Income Today",
    "total_income": "Total Income",
}
