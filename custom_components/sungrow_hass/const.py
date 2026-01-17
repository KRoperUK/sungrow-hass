"""Constants for the Sungrow iSolarCloud integration."""

DOMAIN = "sungrow_hass"
CONF_APP_KEY = "app_key"
CONF_APP_SECRET = "app_secret"
CONF_APP_ID = "app_id"
CONF_AUTH_URL = "auth_url_input"
CONF_GATEWAY = "gateway"
CONF_REDIRECT_URI = "redirect_uri"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

GATEWAYS = {
    "Europe": "https://gateway.isolarcloud.eu",
    "International": "https://gateway.isolarcloud.com.hk",
    "China": "https://gateway.isolarcloud.com",
    "Australia": "https://augateway.isolarcloud.com",
}
