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

GATEWAYS = {
    "Europe": "https://gateway.isolarcloud.eu",
    "International": "https://gateway.isolarcloud.com.hk",
    "China": "https://gateway.isolarcloud.com",
    "Australia": "https://augateway.isolarcloud.com",
}

DEFAULT_HOST = GATEWAYS["Europe"]

# Polling interval (minutes). iSolarCloud allows ~2000 calls/hour, so the default
# is deliberately conservative; users can tune it via the integration options.
DEFAULT_SCAN_INTERVAL = 5
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 1440
