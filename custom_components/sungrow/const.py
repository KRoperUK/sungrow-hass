"""Constants for the Sungrow iSolarCloud integration."""

DOMAIN = "sungrow"
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
    "5": "mppt1_voltage",
    "6": "mppt1_current",
    "7": "mppt2_voltage",
    "8": "mppt2_current",
    "9": "mppt3_voltage",
    "10": "mppt3_current",
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
}

# The technical/health subset of the battery points shown as diagnostics; the rest
# (SOC, charge/discharge energy) stay primary sensors for the dashboards.
BATTERY_DIAGNOSTIC_CODES = frozenset({"battery_voltage", "battery_current", "battery_temperature", "battery_soh"})

# Communication-module (WiNet-S) device-level measuring points surfaced as diagnostic
# sensors (#149), requested per-device when device sensors are enabled. Both IDs are in
# the measure-point catalog.
COMM_MODULE_POINTS: dict[str, str] = {
    "23014": "wlan_signal_strength",
    "23001": "wireless_signal_strength",
}
