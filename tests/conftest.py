"""Fixtures for Sungrow tests."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dotenv import load_dotenv
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import (
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_PASSWORD,
    CONF_USERNAME,
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
    CONF_USERNAME: "test@example.com",
    CONF_PASSWORD: "test_password",
    CONF_GATEWAY: "Europe",
    "token": "test_token_123",
    "user_id": "12345",
}

MOCK_USER_INPUT = {
    CONF_APP_KEY: "test_app_key",
    CONF_APP_SECRET: "test_app_secret",
    CONF_USERNAME: "test@example.com",
    CONF_PASSWORD: "test_password",
    CONF_GATEWAY: "Europe",
}

MOCK_PLANT_LIST = [
    {"ps_id": 12345, "ps_name": "Test Solar Plant"},
    {"ps_id": 67890, "ps_name": "Second Plant"},
]

MOCK_REALTIME_DATA = {
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
}

MOCK_REALTIME_DATA_SECOND_PLANT = {
    "total_active_power": {
        "code": "total_active_power",
        "value": "3.10",
        "unit": "kW",
        "name": "Total Active Power",
    },
}


@pytest.fixture(autouse=True)
def patch_async_drop_config_annotations():
    """Patch async_drop_config_annotations to handle IntegrationConfigInfo."""
    from homeassistant import config as ha_config

    original_func = ha_config.async_drop_config_annotations

    def side_effect(config, integration):
        if isinstance(config, dict):
            from types import SimpleNamespace

            return original_func(SimpleNamespace(config=config, exception_info_list=[]), integration)

        if hasattr(config, "config") and not isinstance(config.config, dict):
            return {}

        return original_func(config, integration)

    with patch("homeassistant.config.async_drop_config_annotations", side_effect=side_effect):
        yield


# ---------------------------------------------------------------------------
# HA integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations defined in the test dir."""
    yield


@pytest.fixture(autouse=True)
def auto_mock_hass_http(hass: HomeAssistant):
    """Mock hass.http for tests."""
    hass.http = MagicMock()
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG_DATA.copy(),
        title="Sungrow test@example.com",
        unique_id="test@example.com",
    )


# ---------------------------------------------------------------------------
# API client mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_api():
    """Create a mock ISolarCloudAPI instance for config_flow tests."""
    with patch("custom_components.sungrow.config_flow.ISolarCloudAPI") as mock_api_cls:
        api_instance = MagicMock()
        api_instance.async_login = AsyncMock(return_value={"token": "test_token_123", "user_id": "12345"})
        api_instance.token = "test_token_123"
        api_instance.user_id = "12345"
        mock_api_cls.return_value = api_instance
        yield api_instance


@pytest.fixture
def mock_api_invalid_auth():
    """Create a mock ISolarCloudAPI that raises on login."""
    with patch("custom_components.sungrow.config_flow.ISolarCloudAPI") as mock_api_cls:
        from custom_components.sungrow.isolarcloud_api import ISolarCloudError

        api_instance = MagicMock()
        api_instance.async_login = AsyncMock(side_effect=ISolarCloudError("Invalid credentials"))
        mock_api_cls.return_value = api_instance
        yield api_instance


@pytest.fixture
def mock_sensor_api():
    """Create a mock ISolarCloudAPI for sensor setup."""
    with patch("custom_components.sungrow.sensor.ISolarCloudAPI") as mock_api_cls:
        api_instance = MagicMock()
        api_instance.token = "test_token_123"
        api_instance.user_id = "12345"
        api_instance.async_login = AsyncMock(return_value={})
        api_instance.async_get_plant_list = AsyncMock(return_value=MOCK_PLANT_LIST)
        api_instance.async_get_plant_realtime_data = AsyncMock(side_effect=lambda ps_id: {
            "12345": MOCK_REALTIME_DATA,
            "67890": MOCK_REALTIME_DATA_SECOND_PLANT,
        }.get(str(ps_id), {}))
        mock_api_cls.return_value = api_instance
        yield api_instance


# ---------------------------------------------------------------------------
# Live test credentials
# ---------------------------------------------------------------------------


@pytest.fixture
def live_credentials():
    """Return credentials for live tests if available."""
    app_key = os.getenv("SUNGROW_APPKEY")
    app_secret = os.getenv("SUNGROW_APPSECRET")
    username = os.getenv("SUNGROW_USERNAME")
    password = os.getenv("SUNGROW_PASSWORD")
    host = os.getenv("SUNGROW_HOST", "https://gateway.isolarcloud.eu")

    if not all([app_key, app_secret, username, password]):
        pytest.skip("Live test credentials not found in environment variables or .env")

    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "username": username,
        "password": password,
        "host": host,
    }
