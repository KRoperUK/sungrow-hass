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
