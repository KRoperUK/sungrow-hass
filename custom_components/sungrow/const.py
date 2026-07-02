"""Constants for the Sungrow iSolarCloud integration."""

DOMAIN = "sungrow"
CONF_APP_KEY = "app_key"
CONF_APP_SECRET = "app_secret"
CONF_APP_ID = "app_id"
CONF_AUTH_URL = "auth_url_input"
CONF_GATEWAY = "gateway"
CONF_REDIRECT_URI = "redirect_uri"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Options
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXTRA_MEASURE_POINTS = "extra_measure_points"

GATEWAYS = {
    "Europe": "https://gateway.isolarcloud.eu",
    "International": "https://gateway.isolarcloud.com.hk",
    "China": "https://gateway.isolarcloud.com",
    "Australia": "https://augateway.isolarcloud.com",
}

DEFAULT_HOST = GATEWAYS["Europe"]

# Polling interval (seconds). iSolarCloud allows ~2000 calls/hour on the free plan,
# so the minimum is capped at 10 s to prevent accidental quota exhaustion.
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 86400
