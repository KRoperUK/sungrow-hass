"""Fixtures for Sungrow tests."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dotenv import load_dotenv
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_REDIRECT_URI,
    DOMAIN,
)

# Load environment variables from .env file (for live tests)
load_dotenv()


# ---------------------------------------------------------------------------
# Common test data
# ---------------------------------------------------------------------------

MOCK_CONFIG_DATA = {
    CONF_APP_KEY: "test_app_key",
    CONF_APP_SECRET: "test_app_secret",
    CONF_APP_ID: "test_app_id",
    CONF_GATEWAY: "Europe",
    CONF_REDIRECT_URI: "http://homeassistant.local:8123/api/sungrow_hass/callback",
    "tokens": {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_type": "bearer",
    },
}

MOCK_USER_INPUT = {
    CONF_APP_KEY: "test_app_key",
    CONF_APP_SECRET: "test_app_secret",
    CONF_APP_ID: "test_app_id",
    CONF_GATEWAY: "Europe",
    CONF_REDIRECT_URI: "http://homeassistant.local:8123/api/sungrow_hass/callback",
}

MOCK_PLANT_LIST = [
    {"ps_id": 12345, "ps_name": "Test Solar Plant"},
    {"ps_id": 67890, "ps_name": "Second Plant"},
]

MOCK_REALTIME_DATA = {
    "12345": {
        "total_active_power": {
            "code": "total_active_power",
            "value": "5.23",
            "unit": "kW",
            "name": "Total Active Power",
        },
        "daily_energy": {
            "code": "daily_energy",
            "value": "12.45",
            "unit": "kWh",
            "name": "Daily Energy",
        },
        "device_status": {
            "code": "device_status",
            "value": "Running",
            "unit": "",
            "name": "Device Status",
        },
    },
    "67890": {
        "total_active_power": {
            "code": "total_active_power",
            "value": "3.10",
            "unit": "kW",
            "name": "Total Active Power",
        },
    },
}


# ---------------------------------------------------------------------------
# HA integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations defined in the test dir."""
    yield


@pytest.fixture(autouse=True)
def auto_mock_hass_http(hass: HomeAssistant):
    """Mock hass.http so that async_setup can register views without crashing.

    The test HA instance doesn't have an HTTP server, so hass.http is None.
    This also prevents thread leaks from the HTTP server in teardown checks.
    """
    hass.http = MagicMock()
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA.copy(),
        title="Sungrow test_app_id",
        unique_id="test_app_id",
    )


# ---------------------------------------------------------------------------
# pysolarcloud mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_auth():
    """Create a mock Auth instance matching the real pysolarcloud.Auth interface."""
    with patch("custom_components.sungrow.config_flow.Auth") as mock_auth_cls:
        auth_instance = MagicMock()
        auth_instance.auth_url.return_value = "https://isolarcloud.eu/oauth?client_id=test"
        auth_instance.async_authorize = AsyncMock(return_value=None)
        auth_instance.tokens = {
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "token_type": "bearer",
        }
        mock_auth_cls.return_value = auth_instance
        yield auth_instance


@pytest.fixture
def mock_auth_no_tokens(mock_auth):
    """Auth mock where token retrieval returns empty (failed auth)."""
    mock_auth.tokens = {}
    return mock_auth


@pytest.fixture
def mock_plants_service():
    """Create a mock Plants service."""
    with patch("custom_components.sungrow.sensor.Plants") as mock_plants_cls:
        plants_instance = MagicMock()
        plants_instance.async_get_plants = AsyncMock(return_value=MOCK_PLANT_LIST)
        plants_instance.async_get_realtime_data = AsyncMock(return_value=MOCK_REALTIME_DATA)
        mock_plants_cls.return_value = plants_instance
        yield plants_instance


@pytest.fixture
def mock_sensor_auth():
    """Create a mock Auth instance for sensor setup (patches sensor module)."""
    with patch("custom_components.sungrow.sensor.Auth") as mock_auth_cls:
        auth_instance = MagicMock()
        auth_instance.tokens = MOCK_CONFIG_DATA["tokens"]
        mock_auth_cls.return_value = auth_instance
        yield auth_instance


# ---------------------------------------------------------------------------
# Live test credentials
# ---------------------------------------------------------------------------


@pytest.fixture
def live_credentials():
    """Return credentials for live tests if available."""
    app_key = os.getenv("SUNGROW_APPKEY")
    app_secret = os.getenv("SUNGROW_APPSECRET")
    app_id = os.getenv("SUNGROW_APP_ID")
    host = os.getenv("SUNGROW_HOST", "https://gateway.isolarcloud.eu")

    if not all([app_key, app_secret, app_id]):
        pytest.skip("Live test credentials not found in environment variables or .env")

    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "app_id": app_id,
        "host": host,
    }
