"""Tests for sungrow.backfill and sungrow.set_battery_mode services (#287)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.sungrow.const import CONF_TRANSPORT, DOMAIN, TRANSPORT_MODBUS_ONLY
from custom_components.sungrow.services import (
    SERVICE_BACKFILL,
    SERVICE_SET_BATTERY_MODE,
    _resolve_entries,
    async_setup_services,
)
from tests.conftest import MOCK_CONFIG_DATA


def _make_entry(hass, *, entry_id="test_entry", transport="cloud_only", state=ConfigEntryState.LOADED):
    """Create a mock config entry."""
    from unittest.mock import MagicMock as _MagicMock

    entry = _MagicMock()
    entry.entry_id = entry_id
    entry.domain = DOMAIN
    entry.state = state
    entry.data = {**MOCK_CONFIG_DATA, CONF_TRANSPORT: transport}
    entry.runtime_data = _MagicMock()
    entry.runtime_data.backfill = _MagicMock()
    entry.runtime_data.backfill.async_run_on_demand = AsyncMock()
    return entry


# ---------------------------------------------------------------------------
# _resolve_entries
# ---------------------------------------------------------------------------


def test_resolve_entries_all_loaded(hass: HomeAssistant):
    """With no entry_id, returns all loaded cloud entries."""
    e1 = _make_entry(hass, entry_id="e1")
    e2 = _make_entry(hass, entry_id="e2")
    modbus = _make_entry(hass, entry_id="mb", transport=TRANSPORT_MODBUS_ONLY)

    with patch.object(hass.config_entries, "async_entries", return_value=[e1, e2, modbus]):
        result = _resolve_entries(hass, None)

    assert result == [e1, e2]


def test_resolve_entries_specific_entry(hass: HomeAssistant):
    """With an entry_id, resolves that single entry."""
    e1 = _make_entry(hass, entry_id="specific")

    with patch.object(hass.config_entries, "async_get_entry", return_value=e1):
        result = _resolve_entries(hass, "specific")

    assert result == [e1]


def test_resolve_entries_not_found_raises(hass: HomeAssistant):
    """A bad entry_id raises ServiceValidationError."""
    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=None),
        pytest.raises(ServiceValidationError, match="No Sungrow config entry"),
    ):
        _resolve_entries(hass, "nonexistent")


def test_resolve_entries_not_loaded_raises(hass: HomeAssistant):
    """An unloaded entry raises ServiceValidationError."""
    entry = _make_entry(hass, state=ConfigEntryState.NOT_LOADED)
    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=entry),
        pytest.raises(ServiceValidationError, match="not loaded"),
    ):
        _resolve_entries(hass, entry.entry_id)


def test_resolve_entries_modbus_only_raises(hass: HomeAssistant):
    """A Modbus-only entry raises ServiceValidationError."""
    entry = _make_entry(hass, transport=TRANSPORT_MODBUS_ONLY)
    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=entry),
        pytest.raises(ServiceValidationError, match="Modbus"),
    ):
        _resolve_entries(hass, entry.entry_id)


# ---------------------------------------------------------------------------
# Backfill service
# ---------------------------------------------------------------------------


async def test_backfill_service_dispatches_to_manager(hass: HomeAssistant):
    """The backfill service calls async_run_on_demand on addressed entries."""
    entry = _make_entry(hass)

    async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_BACKFILL)

    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        await hass.services.async_call(DOMAIN, SERVICE_BACKFILL, {}, blocking=True)

    entry.runtime_data.backfill.async_run_on_demand.assert_awaited_once_with(plant_ids=None, start_date=None)


async def test_backfill_service_with_start_date(hass: HomeAssistant):
    """A start_date is passed as UTC midnight datetime."""
    entry = _make_entry(hass)

    async_setup_services(hass)

    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        await hass.services.async_call(DOMAIN, SERVICE_BACKFILL, {"start_date": date(2026, 1, 15)}, blocking=True)

    call_kwargs = entry.runtime_data.backfill.async_run_on_demand.call_args.kwargs
    assert call_kwargs["start_date"].date() == date(2026, 1, 15)


async def test_backfill_service_skips_entry_without_manager(hass: HomeAssistant):
    """An entry without a backfill manager is silently skipped."""
    entry = _make_entry(hass)
    entry.runtime_data.backfill = None

    async_setup_services(hass)

    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        # Should not raise.
        await hass.services.async_call(DOMAIN, SERVICE_BACKFILL, {}, blocking=True)


# ---------------------------------------------------------------------------
# set_battery_mode service — target resolution
# ---------------------------------------------------------------------------


async def test_set_battery_mode_no_selects_raises(hass: HomeAssistant):
    """No battery mode selects found raises ServiceValidationError."""
    async_setup_services(hass)
    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {}

    with pytest.raises(ServiceValidationError, match="No Sungrow battery mode"):
        await hass.services.async_call(DOMAIN, SERVICE_SET_BATTERY_MODE, {"mode": "self_consumption"}, blocking=True)


# ---------------------------------------------------------------------------
# sungrow.refresh_tokens (#356)
# ---------------------------------------------------------------------------


def _make_oauth_entry_with_auth(hass, *, entry_id="test_entry", refresh_side_effect=None):
    """Build a mocked cloud entry with a fake ``Auth`` accessible via ``plants_service``."""
    from unittest.mock import MagicMock as _MagicMock

    from custom_components.sungrow.const import TRANSPORT_CLOUD_ONLY

    fake_auth = _MagicMock()
    fake_auth.tokens = {"access_token": "old", "refresh_token": "r", "expires_at": 999999999999}
    fake_auth.async_get_access_token = AsyncMock(side_effect=refresh_side_effect or (lambda: "new"))

    coordinator = _MagicMock()
    coordinator.plants_service = _MagicMock()
    coordinator.plants_service.auth = fake_auth

    entry = _MagicMock()
    entry.entry_id = entry_id
    entry.domain = DOMAIN
    entry.state = ConfigEntryState.LOADED
    entry.data = {**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY}
    entry.runtime_data = _MagicMock()
    entry.runtime_data.coordinators = [coordinator]
    return entry, fake_auth


async def test_refresh_tokens_service_forces_refresh_on_targeted_entry(hass: HomeAssistant):
    """The service invalidates the cached expiry and drives ``Auth.async_get_access_token`` (#356)."""
    from custom_components.sungrow.services import SERVICE_REFRESH_TOKENS

    entry, auth = _make_oauth_entry_with_auth(hass)
    async_setup_services(hass)

    with patch.object(hass.config_entries, "async_get_entry", return_value=entry):
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_TOKENS, {"config_entry": entry.entry_id}, blocking=True)

    # The refresh path was driven — ``expires_at`` was zeroed and access-token
    # fetch was called (the ``Auth`` lock + double-check inside the library
    # performs the actual refresh network call).
    assert auth.tokens["expires_at"] == 0
    auth.async_get_access_token.assert_awaited_once()


async def test_refresh_tokens_service_defaults_to_all_cloud_entries(hass: HomeAssistant):
    """With no ``config_entry`` target, every loaded OAuth entry is refreshed."""
    from custom_components.sungrow.services import SERVICE_REFRESH_TOKENS

    e1, a1 = _make_oauth_entry_with_auth(hass, entry_id="e1")
    e2, a2 = _make_oauth_entry_with_auth(hass, entry_id="e2")
    modbus = _make_entry(hass, entry_id="mb", transport=TRANSPORT_MODBUS_ONLY)
    async_setup_services(hass)

    with patch.object(hass.config_entries, "async_entries", return_value=[e1, e2, modbus]):
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_TOKENS, {}, blocking=True)

    # Both OAuth entries refreshed; the Modbus-only entry was excluded from the sweep.
    a1.async_get_access_token.assert_awaited_once()
    a2.async_get_access_token.assert_awaited_once()


async def test_refresh_tokens_service_rejects_modbus_only(hass: HomeAssistant):
    """A Modbus-only target raises ``ServiceValidationError`` — no tokens to refresh."""
    from custom_components.sungrow.services import SERVICE_REFRESH_TOKENS

    modbus = _make_entry(hass, transport=TRANSPORT_MODBUS_ONLY)
    async_setup_services(hass)

    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=modbus),
        pytest.raises(ServiceValidationError, match="no OAuth tokens"),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_TOKENS, {"config_entry": modbus.entry_id}, blocking=True)


async def test_refresh_tokens_service_rejects_cloud_user(hass: HomeAssistant):
    """A cloud_user target raises ``ServiceValidationError`` — no OAuth refresh token."""
    from custom_components.sungrow.const import TRANSPORT_CLOUD_USER
    from custom_components.sungrow.services import SERVICE_REFRESH_TOKENS

    entry = _make_entry(hass, transport=TRANSPORT_CLOUD_USER)
    async_setup_services(hass)

    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=entry),
        pytest.raises(ServiceValidationError, match="user-account"),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_TOKENS, {"config_entry": entry.entry_id}, blocking=True)


async def test_refresh_tokens_service_surfaces_refresh_failure(hass: HomeAssistant):
    """A failing refresh is re-raised as :class:`HomeAssistantError` for the UI to display."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.sungrow.services import SERVICE_REFRESH_TOKENS

    async def _fail() -> str:
        raise RuntimeError("upstream 500")

    entry, auth = _make_oauth_entry_with_auth(hass, refresh_side_effect=_fail)
    async_setup_services(hass)

    with (
        patch.object(hass.config_entries, "async_get_entry", return_value=entry),
        pytest.raises(HomeAssistantError, match="upstream 500"),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_TOKENS, {"config_entry": entry.entry_id}, blocking=True)


async def test_refresh_tokens_service_no_entries_raises(hass: HomeAssistant):
    """With no loaded OAuth entries at all, the service surfaces a clear validation error."""
    from custom_components.sungrow.services import SERVICE_REFRESH_TOKENS

    async_setup_services(hass)

    with (
        patch.object(hass.config_entries, "async_entries", return_value=[]),
        pytest.raises(ServiceValidationError, match="No loaded OAuth"),
    ):
        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_TOKENS, {}, blocking=True)
