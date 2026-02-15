"""Tests for Sungrow component setup and the auth callback view."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import (
    SungrowAuthCallbackView,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.sungrow.const import DOMAIN

from .conftest import MOCK_CONFIG_DATA


# ---------------------------------------------------------------------------
# async_setup (registers the HTTP callback view)
# ---------------------------------------------------------------------------


async def test_async_setup_registers_callback_view(hass: HomeAssistant):
    """Test async_setup registers the SungrowAuthCallbackView."""
    result = await async_setup(hass, {})

    assert result is True
    # Verify register_view was called with a SungrowAuthCallbackView instance
    hass.http.register_view.assert_called_once()
    view_arg = hass.http.register_view.call_args[0][0]
    assert isinstance(view_arg, SungrowAuthCallbackView)


# ---------------------------------------------------------------------------
# async_setup_entry / async_unload_entry
# ---------------------------------------------------------------------------


async def test_async_setup_entry(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test a successful setup entry stores data and forwards platforms."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sungrow.sensor.async_setup_entry",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]


async def test_async_unload_entry(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test successful unload removes data."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sungrow.sensor.async_setup_entry",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_async_setup_entry_stores_data(hass: HomeAssistant, mock_sensor_auth, mock_plants_service):
    """Test that setup stores entry data under hass.data[DOMAIN][entry_id]."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)

    with patch(
        "custom_components.sungrow.sensor.async_setup_entry",
        return_value=True,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    stored = hass.data[DOMAIN][entry.entry_id]
    assert stored == MOCK_CONFIG_DATA


# ---------------------------------------------------------------------------
# SungrowAuthCallbackView
# ---------------------------------------------------------------------------


class TestSungrowAuthCallbackView:
    """Tests for the OAuth callback HTTP view."""

    def setup_method(self):
        self.view = SungrowAuthCallbackView()

    def test_view_properties(self):
        """Test view URL, name, and auth requirement."""
        assert self.view.url == "/api/sungrow_hass/callback"
        assert self.view.name == "api:sungrow_hass:callback"
        assert self.view.requires_auth is False

    async def test_callback_missing_code(self, hass: HomeAssistant):
        """Test callback returns 400 when code is missing."""
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?flow_id=abc")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400
        assert "Missing code or flow_id" in response.text

    async def test_callback_missing_flow_id(self, hass: HomeAssistant):
        """Test callback returns 400 when flow_id is missing."""
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback?code=abc")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400
        assert "Missing code or flow_id" in response.text

    async def test_callback_missing_both_params(self, hass: HomeAssistant):
        """Test callback returns 400 when both params are missing."""
        mock_request = make_mocked_request("GET", "/api/sungrow_hass/callback")
        mock_request.app["hass"] = hass

        response = await self.view.get(mock_request)

        assert response.status == 400

    async def test_callback_success(self, hass: HomeAssistant):
        """Test a successful callback configures the flow."""
        mock_request = make_mocked_request(
            "GET", "/api/sungrow_hass/callback?code=auth_code_123&flow_id=flow_abc"
        )
        mock_request.app["hass"] = hass

        with patch.object(
            hass.config_entries.flow,
            "async_configure",
            new_callable=AsyncMock,
            return_value={"type": "create_entry"},
        ) as mock_configure:
            response = await self.view.get(mock_request)

        assert response.status == 200
        assert "Authorization successful" in response.text
        mock_configure.assert_called_once_with(
            flow_id="flow_abc", user_input={"code": "auth_code_123"}
        )

    async def test_callback_flow_error(self, hass: HomeAssistant):
        """Test callback returns 500 when flow configuration fails."""
        mock_request = make_mocked_request(
            "GET", "/api/sungrow_hass/callback?code=auth_code&flow_id=bad_flow"
        )
        mock_request.app["hass"] = hass

        with patch.object(
            hass.config_entries.flow,
            "async_configure",
            new_callable=AsyncMock,
            side_effect=Exception("Flow not found"),
        ):
            response = await self.view.get(mock_request)

        assert response.status == 500
        assert "Error occurred" in response.text
