"""Constants for the Sungrow iSolarCloud integration."""

from datetime import timedelta

from pysolarcloud.plants import DeviceType

DOMAIN = "sungrow"
VERSION = "4.1.0"  # x-release-please-version
CONF_APP_KEY = "app_key"
CONF_APP_SECRET = "app_secret"
CONF_APP_ID = "app_id"
CONF_GATEWAY = "gateway"
CONF_REDIRECT_URI = "redirect_uri"

# Options
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXTRA_MEASURE_POINTS = "extra_measure_points"
# Opt-in: also poll each discovered device (charger, meter, extra battery) for its
# own realtime points and expose them as sensors under that device. Off by default
# to avoid extra API calls / entity clutter for users who only need plant data.
CONF_ENABLE_DEVICE_SENSORS = "enable_device_sensors"
# Opt-in local Modbus transport (#159): the WiNet-S dongle's IP/host. When set, the
# coordinator reads fast local values over Modbus TCP and merges them over the cloud
# data (Modbus preferred). Empty -> cloud only (default). Port/unit default to 502/1.
CONF_MODBUS_HOST = "modbus_host"
CONF_MODBUS_PORT = "modbus_port"
CONF_MODBUS_UNIT = "modbus_unit"
# Opt-in: expose the raw Modbus register window on the daily_yield sensor as
# ``daily_yield_diagnostic`` (large attribute; off by default to protect the recorder).
CONF_MODBUS_DEBUG_DAILY_YIELD = "modbus_debug_daily_yield"
DEFAULT_MODBUS_PORT = 502
DEFAULT_MODBUS_UNIT = 1
# Entry-data marker for a fully local, cloud-free entry created from zeroconf
# discovery of a WiNet-S (#159). Such an entry carries no cloud credentials; its
# realtime data comes entirely from local Modbus.
CONF_TRANSPORT = "transport"
TRANSPORT_CLOUD_ONLY = "cloud_only"
TRANSPORT_CLOUD_MODBUS = "cloud_modbus"
TRANSPORT_MODBUS_ONLY = "modbus_only"
# Discovered WiNet-S identity stored on a Modbus-only entry.
CONF_MODEL = "model"
CONF_SERIAL = "serial"
DEFAULT_MODBUS_SCAN_INTERVAL = 30

# Backfill historical statistics (see specs/backfill-historical-statistics).
# The History_Window defaults to 30 days back from now, is user-configurable via the
# CONF_BACKFILL_DAYS option, and is clamped to [1, 365] days so a run can never request
# an unbounded range.
CONF_BACKFILL_DAYS = "backfill_days"
DEFAULT_BACKFILL_DAYS = 30
MAX_BACKFILL_DAYS = 365
# The historical endpoint returns ~5-minute cadence rows; request that interval and
# split the window into 3-hour Time_Chunks to stay within the per-call query window.
BACKFILL_INTERVAL = timedelta(minutes=5)
BACKFILL_CHUNK_WINDOW = timedelta(hours=3)
# At most 50 Backfill_Points per Historical_Data_API call (endpoint per-call point cap).
MAX_POINTS_PER_CALL = 50
# Minimum seconds between historical calls (throttle) and the transient-error retry cap.
BACKFILL_MIN_CALL_INTERVAL = 1.0
BACKFILL_MAX_RETRIES = 3

GATEWAYS = {
    "Europe": "https://gateway.isolarcloud.eu",
    "International": "https://gateway.isolarcloud.com.hk",
    "China": "https://gateway.isolarcloud.com",
    "Australia": "https://augateway.isolarcloud.com",
}

# Web console URL per region, used as the device `configuration_url` so the
# "Visit device" link points at the right regional iSolarCloud portal.
GATEWAY_CONSOLE_URLS = {
    "Europe": "https://isolarcloud.eu",
    "International": "https://isolarcloud.com.hk",
    "China": "https://isolarcloud.com",
    "Australia": "https://au.isolarcloud.com",
}

DEFAULT_HOST = GATEWAYS["Europe"]
DEFAULT_CONSOLE_URL = GATEWAY_CONSOLE_URLS["Europe"]

# Polling interval (seconds). iSolarCloud allows ~2000 calls/hour on the free plan,
# so the minimum is capped at 10 s to prevent accidental quota exhaustion.
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 86400

# How often (seconds) to re-fetch a plant's device list while polling. The device
# set changes rarely, so re-listing it on every realtime poll needlessly burns
# calls against the ~2000/hour free-plan cap. Refresh at most this often (plus
# always on the first poll) so newly added/removed devices are still picked up.
DEVICE_REFRESH_INTERVAL = 900

# Operating-status measuring point per inverter family (#182). Always requested — even
# when per-device sensors are off — so the Fault binary sensor can surface a
# human-readable reason ("Shut down due to faults", "Low insulation resistance", ...).
# Inverters report status on point 29, ESS/hybrids on 13146; both resolve via the shared
# operating-status enum.
INVERTER_OPERATING_STATUS_POINT: dict[str, str] = {"29": "operating_status"}
ESS_OPERATING_STATUS_POINT: dict[str, str] = {"13146": "operating_status"}

# Battery charge/discharge power measuring points for energy-storage inverters (#31).
# Always requested for ESS devices (even with per-device sensors off) so hybrid users
# see separate charge and discharge power sensors without manual configuration. The
# signed plant-level ``total_field_energy_storage_active_power`` is already present, but
# many users expect dedicated charge/discharge readings.
ESS_BATTERY_POWER_POINTS: dict[str, str] = {
    "13126": "battery_charge_power",
    "13150": "battery_discharge_power",
}

# String-inverter MPPT voltage/current (points 5-10, MPPT1-3). Named so the per-model
# capability resolver (#251) and the ESS branch can reference the exact string-inverter
# MPPT id set instead of a duplicated literal. Merged into INVERTER_DIAGNOSTIC_POINTS
# below so the diagnostic map is unchanged.
STRING_MPPT_POINTS: dict[str, str] = {
    "5": "mppt1_voltage",
    "6": "mppt1_current",
    "7": "mppt2_voltage",
    "8": "mppt2_current",
    "9": "mppt3_voltage",
    "10": "mppt3_current",
}

# Inverter/ESS device-level measuring points surfaced as diagnostic sensors (#149),
# requested per-device when device sensors are enabled. Maps the documented inverter
# point ID -> a stable code. Every ID is already in the measure-point catalog, so it
# classifies automatically (29 -> operating-status enum, 14 -> DC power, 5-10 -> MPPT
# voltage/current, 4 -> temperature, 27 -> frequency, 94 -> insulation resistance).
INVERTER_DIAGNOSTIC_POINTS: dict[str, str] = {
    "29": "operating_status",
    "14": "total_dc_power",
    "4": "internal_temperature",
    "27": "grid_frequency",
    "94": "array_insulation_resistance",
    # String-inverter MPPT voltage/current (points 5-10) — see STRING_MPPT_POINTS above.
    **STRING_MPPT_POINTS,
    # Grid-side health (#179). Per-phase voltages/currents, power quality and DC-link
    # voltage — all live-confirmed on real hardware except the AFCI/insulation extras,
    # which the per-device builder simply skips when a model doesn't report them.
    "3": "total_running_time",
    "18": "phase_a_voltage",
    "19": "phase_b_voltage",
    "20": "phase_c_voltage",
    "21": "phase_a_current",
    "22": "phase_b_current",
    "23": "phase_c_current",
    "25": "reactive_power",
    "26": "power_factor",
    "43": "apparent_power",
    "95": "bus_voltage",
    "90": "negative_voltage_to_ground",
    "120": "afci_fault_count",
    # Per-string DC voltage/current for array analysis (#189). Live-confirmed on a
    # 2-string SG3.6RS (strings 1-2 populated, 3-8 skipped). Strings 1-8 cover
    # residential + small-commercial arrays; a string a model lacks returns nothing.
    "96": "string_1_voltage",
    "70": "string_1_current",
    "97": "string_2_voltage",
    "71": "string_2_current",
    "98": "string_3_voltage",
    "72": "string_3_current",
    "99": "string_4_voltage",
    "73": "string_4_current",
    "100": "string_5_voltage",
    "74": "string_5_current",
    "101": "string_6_voltage",
    "75": "string_6_current",
    "102": "string_7_voltage",
    "76": "string_7_current",
    "103": "string_8_voltage",
    "77": "string_8_current",
}

# SH-family hybrids/ESS report MPPT voltage/current on a separate point-ID range
# than string inverters (#189 follow-up). Reuse the same mpptN_* code names so the
# existing classification/naming/icon path treats them identically; only the point
# IDs differ. These are already in the measure-point catalog (units V/A), so they
# classify by unit automatically.
ESS_MPPT_DIAGNOSTIC_POINTS: dict[str, str] = {
    "13001": "mppt1_voltage",
    "13002": "mppt1_current",
    "13105": "mppt2_voltage",
    "13106": "mppt2_current",
    "13107": "mppt3_voltage",
    "13108": "mppt3_current",
    "13109": "mppt4_voltage",
    "13110": "mppt4_current",
}

# Battery/ESS device-level measuring points surfaced as sensors for hybrid users (#154),
# requested per-device when device sensors are enabled. Every ID is already in the
# measure-point catalog, so it classifies automatically (level -> battery, charge/discharge
# energy -> total_increasing, voltage/current/temperature accordingly).
BATTERY_DEVICE_POINTS: dict[str, str] = {
    "58604": "battery_level",
    "58606": "battery_total_charge_energy",
    "58607": "battery_total_discharge_energy",
    "58601": "battery_voltage",
    "58602": "battery_current",
    "58603": "battery_temperature",
    "58605": "battery_soh",
    # Cell/module-level health (#180): imbalance and thermal early-warning. Min/max
    # cell voltage flags a weak/failing cell; min/max module temperature flags a
    # thermal spread; the status/contactor/fault-module points aid fault diagnosis.
    "58608": "battery_operation_status",
    "58610": "battery_max_cell_voltage",
    "58612": "battery_min_cell_voltage",
    "58614": "battery_max_module_temperature",
    "58616": "battery_min_module_temperature",
    "58635": "battery_dc_contactor_status",
    "58636": "battery_fault_module_id",
}

# The technical/health subset of the battery points shown as diagnostics; the rest
# (SOC, charge/discharge energy) stay primary sensors for the dashboards.
BATTERY_DIAGNOSTIC_CODES = frozenset(
    {
        "battery_voltage",
        "battery_current",
        "battery_temperature",
        "battery_soh",
        "battery_operation_status",
        "battery_max_cell_voltage",
        "battery_min_cell_voltage",
        "battery_max_module_temperature",
        "battery_min_module_temperature",
        "battery_dc_contactor_status",
        "battery_fault_module_id",
    }
)

# Communication-module (WiNet-S) device-level measuring points surfaced as diagnostic
# sensors (#149), requested per-device when device sensors are enabled. Both IDs are in
# the measure-point catalog.
COMM_MODULE_POINTS: dict[str, str] = {
    "23014": "wlan_signal_strength",
    "23001": "wireless_signal_strength",
}

# Energy-meter (device_type 7) measuring points surfaced as sensors when device sensors
# are enabled (#179). Instantaneous power/PF/frequency and per-phase V/I/energy that the
# plant-level realtime aggregate doesn't carry. Model-dependent — a meter that only
# reports energy simply returns nothing for the power/phase points and they're skipped.
METER_DEVICE_POINTS: dict[str, str] = {
    "8018": "meter_active_power",
    "8022": "meter_reactive_power",
    "8026": "meter_apparent_power",
    "8014": "meter_power_factor",
    "8064": "meter_frequency",
    "8000": "meter_phase_a_voltage",
    "8001": "meter_phase_b_voltage",
    "8002": "meter_phase_c_voltage",
    "8006": "meter_phase_a_current",
    "8007": "meter_phase_b_current",
    "8008": "meter_phase_c_current",
    "8030": "meter_forward_active_energy",
    "8031": "meter_reverse_active_energy",
}

# Physical-device modelling (#158): map a plant realtime point code to the device
# type(s) that own it. A sensor re-homes onto that device only when the plant has
# exactly one matching device (see resolve_point_device); otherwise it stays on the
# plant device. Grounded in the 74 real codes from a live plant + docs/SENSORS.md.
# Unmapped codes (load, plant aggregates, ratios, forecasts) intentionally stay on
# the plant, since a household load is not a device and ratios/forecasts are analytics.
_PV_TYPES = frozenset(
    {
        DeviceType.INVERTER,
        DeviceType.MICROINVERTER,
        DeviceType.ENERGY_STORAGE_SYSTEM,
        DeviceType.ENERGY_STORAGE_SYSTEM_2,
    }
)
_BATTERY_TYPES = frozenset({DeviceType.ENERGY_STORAGE_SYSTEM, DeviceType.ENERGY_STORAGE_SYSTEM_2, DeviceType.BATTERY})
_METER_TYPES = frozenset({DeviceType.METER, DeviceType.GRID_CONNECTION_POINT})

POINT_DEVICE_TYPE: dict[str, frozenset[DeviceType]] = {
    # PV / inverter
    "total_active_power": _PV_TYPES,
    "total_active_power_of_pv": _PV_TYPES,
    "inverter_ac_power": _PV_TYPES,
    "inverter_ac_power_normalization": _PV_TYPES,
    "inverter_daily_yield": _PV_TYPES,
    "inverter_total_yield": _PV_TYPES,
    "inverter_pr": _PV_TYPES,
    "daily_yield": _PV_TYPES,
    "total_yield": _PV_TYPES,
    "total_pv_yield": _PV_TYPES,
    "daily_pv_yield_ems": _PV_TYPES,
    "pv_active_power_ems": _PV_TYPES,
    "total_dc_power": _PV_TYPES,
    "daily_equivalent_hours_of_inverter": _PV_TYPES,
    "phase_a_voltage": _PV_TYPES,
    "phase_b_voltage": _PV_TYPES,
    "phase_c_voltage": _PV_TYPES,
    "reactive_power": _PV_TYPES,
    "power_factor": _PV_TYPES,
    # Battery / ESS
    "battery_level_soc": _BATTERY_TYPES,
    "battery_soc": _BATTERY_TYPES,
    "total_field_soc": _BATTERY_TYPES,
    "energy_storage_soc_ems": _BATTERY_TYPES,
    "total_field_energy_storage_active_power": _BATTERY_TYPES,
    "total_field_energy_storage_maximum_reactive_power": _BATTERY_TYPES,
    "total_field_maximum_rechargeable_power": _BATTERY_TYPES,
    "total_field_maximum_dischargeable_power": _BATTERY_TYPES,
    "total_field_chargeable_energy": _BATTERY_TYPES,
    "total_field_dischargeable_energy": _BATTERY_TYPES,
    "total_field_charge_capacity": _BATTERY_TYPES,
    "total_field_discharge_capacity": _BATTERY_TYPES,
    "daily_field_charge_capacity": _BATTERY_TYPES,
    "daily_field_discharge_capacity": _BATTERY_TYPES,
    "total_field_power_factor": _BATTERY_TYPES,
    "total_field_reactive_power": _BATTERY_TYPES,
    "total_number_of_charge_discharge": _BATTERY_TYPES,
    "energy_storage_active_power_ems": _BATTERY_TYPES,
    "energy_storage_cumulative_charge": _BATTERY_TYPES,
    "energy_storage_remaining_charge": _BATTERY_TYPES,
    "energy_storage_remaining_charge_ems": _BATTERY_TYPES,
    "ess_daily_charge_ems": _BATTERY_TYPES,
    "ess_daily_discharge_ems": _BATTERY_TYPES,
    "cumulative_discharge": _BATTERY_TYPES,
    "planned_charging_power": _BATTERY_TYPES,
    "planned_discharging_power": _BATTERY_TYPES,
    "planned_es_charging_discharging_power": _BATTERY_TYPES,
    "planned_es_soc": _BATTERY_TYPES,
    "battery_charge_power": _BATTERY_TYPES,
    "battery_discharge_power": _BATTERY_TYPES,
    "battery_level": _BATTERY_TYPES,
    "battery_soh": _BATTERY_TYPES,
    "battery_voltage": _BATTERY_TYPES,
    "battery_current": _BATTERY_TYPES,
    "battery_temperature": _BATTERY_TYPES,
    "battery_total_charge_energy": _BATTERY_TYPES,
    "battery_total_discharge_energy": _BATTERY_TYPES,
    # Meter / grid
    "grid_active_power": _METER_TYPES,
    "grid_active_power_ems": _METER_TYPES,
    "meter_ac_power": _METER_TYPES,
    "meter_active_power": _METER_TYPES,
    "meter_daily_yield": _METER_TYPES,
    "meter_total_yield": _METER_TYPES,
    "meter_e_daily_consumption": _METER_TYPES,
    "accumulative_power_consumption_by_meter": _METER_TYPES,
    "feed_in_energy_today": _METER_TYPES,
    "feed_in_energy_total": _METER_TYPES,
    "daily_feed_in_energy_pv": _METER_TYPES,
    "energy_purchased_today": _METER_TYPES,
    "total_purchased_energy": _METER_TYPES,
    "meter_forward_active_energy": _METER_TYPES,
    "meter_reverse_active_energy": _METER_TYPES,
    "meter_daily_forward_active_energy": _METER_TYPES,
    "meter_daily_reverse_active_energy": _METER_TYPES,
    "meter_apparent_power": _METER_TYPES,
    "meter_frequency": _METER_TYPES,
}
