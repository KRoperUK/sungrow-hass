"""The on-demand Backfill service for the Sungrow iSolarCloud integration.

Registers ``sungrow.backfill`` as an admin service. A call resolves the addressed cloud
config entry (or every loaded cloud entry when none is given), reads its
``runtime_data.backfill`` manager and asks it to run on demand. An optional ``start_date``
(a date) is interpreted as UTC midnight and passed straight through as the run's window
start override.

See ``.kiro/specs/backfill-historical-statistics`` for the design and requirements
(Requirements 2.1, 2.2, 2.3).
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .const import CONF_TRANSPORT, DOMAIN, TRANSPORT_MODBUS_ONLY

_LOGGER = logging.getLogger(__name__)

SERVICE_BACKFILL = "backfill"
ATTR_CONFIG_ENTRY = "config_entry"
ATTR_START_DATE = "start_date"

_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_START_DATE): cv.date,
    }
)


def _cloud_backfill_entries(hass: HomeAssistant) -> list[Any]:
    """Return every loaded cloud Sungrow config entry (those that own a manager)."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and entry.data.get(CONF_TRANSPORT) != TRANSPORT_MODBUS_ONLY
    ]


def _resolve_entries(hass: HomeAssistant, entry_id: str | None) -> list[Any]:
    """Resolve the addressed cloud entries, defaulting to all loaded cloud entries."""
    if entry_id is None:
        return _cloud_backfill_entries(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(f"No Sungrow config entry found for '{entry_id}'")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"Sungrow config entry '{entry_id}' is not loaded")
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS_ONLY:
        raise ServiceValidationError(f"Config entry '{entry_id}' is a local Modbus entry; Backfill is cloud-only")
    return [entry]


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the ``sungrow.backfill`` admin service exactly once (Requirement 2.1)."""
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _handle_backfill(call: ServiceCall) -> None:
        """Dispatch an on-demand Backfill to the addressed cloud entries' managers."""
        entry_id = call.data.get(ATTR_CONFIG_ENTRY)
        start_date = call.data.get(ATTR_START_DATE)
        # A date selector yields a date; interpret it as UTC midnight (Requirement 2.3).
        start_override: datetime | None = None
        if start_date is not None:
            start_override = datetime.combine(start_date, time.min, tzinfo=dt_util.UTC)

        entries = _resolve_entries(hass, entry_id)
        for entry in entries:
            manager = getattr(entry.runtime_data, "backfill", None)
            if manager is None:
                _LOGGER.debug("Skipping Backfill for entry %s: no manager", entry.entry_id)
                continue
            await manager.async_run_on_demand(plant_ids=None, start_date=start_override)

    async_register_admin_service(hass, DOMAIN, SERVICE_BACKFILL, _handle_backfill, schema=_SERVICE_SCHEMA)
    _LOGGER.debug("Registered %s.%s service", DOMAIN, SERVICE_BACKFILL)
