"""Home Assistant services for the Sungrow iSolarCloud integration.

* ``sungrow.backfill`` — on-demand historical statistics import (admin).
* ``sungrow.set_battery_mode`` — set the unified battery mode for tariff/automation
  dispatch (#255).
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .const import CONF_TRANSPORT, DOMAIN, TRANSPORT_MODBUS_ONLY

_LOGGER = logging.getLogger(__name__)

SERVICE_BACKFILL = "backfill"
SERVICE_SET_BATTERY_MODE = "set_battery_mode"
ATTR_CONFIG_ENTRY = "config_entry"
ATTR_START_DATE = "start_date"
ATTR_MODE = "mode"
ATTR_DURATION_MINUTES = "duration_minutes"

# Keep in sync with select.BATTERY_MODE_SERVICE_KEYS (avoid circular import with select).
_BATTERY_MODE_KEYS = ("self_consumption", "force_charge", "force_discharge", "stop")
_BATTERY_MODE_PARAM = "battery_mode"

_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_START_DATE): cv.date,
    }
)

_SET_BATTERY_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MODE): vol.In(list(_BATTERY_MODE_KEYS)),
        vol.Optional(ATTR_DURATION_MINUTES): vol.All(vol.Coerce(float), vol.Range(min=0, max=1440)),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
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


def _battery_mode_registry(hass: HomeAssistant) -> dict[str, Any]:
    """Return the live battery-mode select registry (entity_id → entity)."""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return {}
    registry = domain_data.get("battery_mode_selects")
    return registry if isinstance(registry, dict) else {}


def _resolve_battery_mode_selects(hass: HomeAssistant, call: ServiceCall) -> list[Any]:
    """Resolve battery-mode select entities from the service call targets (#255)."""
    # Local import avoids a circular import with select → __init__ → services.
    from .select import SungrowDispatchSelect  # noqa: PLC0415

    registry = _battery_mode_registry(hass)
    entity_ids: set[str] = set(call.data.get(ATTR_ENTITY_ID) or [])

    for device_id in call.data.get(ATTR_DEVICE_ID) or []:
        ent_reg = er.async_get(hass)
        for ent in er.async_entries_for_device(ent_reg, device_id):
            if ent.domain == "select" and ent.platform == DOMAIN and (ent.unique_id or "").endswith(
                f"_{_BATTERY_MODE_PARAM}"
            ):
                entity_ids.add(ent.entity_id)

    entry_id = call.data.get(ATTR_CONFIG_ENTRY)
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"No Sungrow config entry found for '{entry_id}'")
        ent_reg = er.async_get(hass)
        for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            if ent.domain == "select" and ent.platform == DOMAIN and (ent.unique_id or "").endswith(
                f"_{_BATTERY_MODE_PARAM}"
            ):
                entity_ids.add(ent.entity_id)

    if not entity_ids:
        # No target: every registered battery-mode select (typical single-plant home).
        all_selects = [s for s in registry.values() if isinstance(s, SungrowDispatchSelect)]
        if not all_selects:
            raise ServiceValidationError("No Sungrow battery mode select entities found")
        return all_selects

    matched: list[Any] = []
    for eid in entity_ids:
        select = registry.get(eid)
        if not isinstance(select, SungrowDispatchSelect):
            raise ServiceValidationError(f"Entity '{eid}' is not a loaded Sungrow battery mode select")
        matched.append(select)
    return matched


def async_setup_services(hass: HomeAssistant) -> None:
    """Register Sungrow services exactly once."""
    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):

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

        async_register_admin_service(hass, DOMAIN, SERVICE_BACKFILL, _handle_backfill, schema=_BACKFILL_SCHEMA)
        _LOGGER.debug("Registered %s.%s service", DOMAIN, SERVICE_BACKFILL)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_BATTERY_MODE):

        async def _handle_set_battery_mode(call: ServiceCall) -> None:
            """Set battery mode on the targeted plant(s) (#255)."""
            from .select import BATTERY_MODE_PARAM, BATTERY_MODE_SERVICE_KEYS  # noqa: PLC0415

            mode_key = call.data[ATTR_MODE]
            option = BATTERY_MODE_SERVICE_KEYS[mode_key]
            duration = call.data.get(ATTR_DURATION_MINUTES)
            selects = _resolve_battery_mode_selects(hass, call)
            for select in selects:
                try:
                    await select.async_select_option(option, duration_minutes=duration)
                    # Best-effort UI refresh when the entity is fully platform-bound.
                    if select.hass is not None and getattr(select, "platform", None) is not None:
                        select.async_write_ha_state()
                except HomeAssistantError:
                    raise
                except Exception as err:  # pragma: no cover - defensive
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="dispatch_write_failed",
                        translation_placeholders={"param": BATTERY_MODE_PARAM, "error": str(err)},
                    ) from err

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_BATTERY_MODE,
            _handle_set_battery_mode,
            schema=_SET_BATTERY_MODE_SCHEMA,
        )
        _LOGGER.debug("Registered %s.%s service", DOMAIN, SERVICE_SET_BATTERY_MODE)
