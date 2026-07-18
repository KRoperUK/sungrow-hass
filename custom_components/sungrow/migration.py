"""Config-entry version migration and legacy hybrid entry splitting.

Extracted from ``__init__.py`` (#289). ``async_migrate_entry`` is re-exported
from the package root so Home Assistant can discover it.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_APP_ID,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_MODBUS,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

_LOGGER = logging.getLogger(__name__)

_HYBRID_OPTION_KEYS = frozenset(
    {
        CONF_MODBUS_HOST,
        "modbus_port",
        "modbus_unit",
        "modbus_debug_daily_yield",
    }
)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry versions to the current schema."""
    if config_entry.version == 1:
        # v1 stored scan_interval in minutes; v2 uses seconds.
        old_interval = config_entry.options.get(CONF_SCAN_INTERVAL, 5)
        new_options = {**config_entry.options, CONF_SCAN_INTERVAL: old_interval * 60}
        hass.config_entries.async_update_entry(config_entry, options=new_options, version=2)
        _LOGGER.info("Migrated scan_interval from %d minutes to %d seconds", old_interval, old_interval * 60)

    if config_entry.version == 2:
        # v2→v3: back-fill transport field for existing entries.
        new_data = dict(config_entry.data)
        if CONF_TRANSPORT not in new_data:
            new_data[CONF_TRANSPORT] = TRANSPORT_CLOUD_ONLY
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)
        _LOGGER.info(
            "Migrated config entry %s to version 3 (transport=%s)", config_entry.title, new_data[CONF_TRANSPORT]
        )

    # Defensive back-fill: ensure cloud entries carry app_id (#245).
    # The unique_id for cloud entries IS the app_id (set during initial setup).
    # If the data key was lost (corrupt storage, partial migration, older RC builds)
    # we can recover it from unique_id. Kept here as a safety net even though
    # async_setup_entry also back-fills, since migration runs before setup.
    if config_entry.version >= 3:
        data = dict(config_entry.data)
        transport = data.get(CONF_TRANSPORT)
        if transport != TRANSPORT_MODBUS_ONLY and not data.get(CONF_APP_ID):
            uid = config_entry.unique_id
            if uid and not uid.startswith("modbus_"):
                data[CONF_APP_ID] = uid
                hass.config_entries.async_update_entry(config_entry, data=data)
                _LOGGER.info(
                    "Back-filled missing app_id from unique_id for entry %s",
                    config_entry.title,
                )

    return True


def _async_split_legacy_hybrid(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Strip hybrid Modbus settings from a cloud entry and spawn local entries.

    Older builds stored ``modbus_host`` on the cloud entry and merged values. That
    mashup is gone: cloud stays pure, and a separate Modbus-only entry is created
    per known inverter serial when possible.
    """
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS_ONLY:
        return
    # cloud_modbus entries legitimately carry modbus_host — don't strip it (#216).
    if entry.data.get(CONF_TRANSPORT) == TRANSPORT_CLOUD_MODBUS:
        return
    host = str(entry.options.get(CONF_MODBUS_HOST) or entry.data.get(CONF_MODBUS_HOST) or "").strip()
    has_hybrid_keys = any(k in entry.options or k in entry.data for k in _HYBRID_OPTION_KEYS)
    if not host and not has_hybrid_keys:
        return

    debug = bool(entry.options.get("modbus_debug_daily_yield", False))
    new_options = {k: v for k, v in entry.options.items() if k not in _HYBRID_OPTION_KEYS}
    new_data = {k: v for k, v in entry.data.items() if k not in _HYBRID_OPTION_KEYS}
    if new_options != dict(entry.options) or new_data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)
        _LOGGER.info(
            "Removed hybrid Modbus settings from cloud entry %s; use a separate local entry instead",
            entry.title,
        )

    if not host:
        return

    registry = dr.async_get(hass)
    serials: list[tuple[str, str]] = []
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        sn = device.serial_number
        if not sn:
            continue
        model = device.model or "Inverter"
        serials.append((sn, str(model)))
    # de-dupe by serial, keep first model
    seen: set[str] = set()
    unique_serials: list[tuple[str, str]] = []
    for sn, model in serials:
        if sn in seen:
            continue
        seen.add(sn)
        unique_serials.append((sn, model))

    if not unique_serials:
        _LOGGER.warning(
            "Cloud entry %s had modbus_host=%s but no inverter serial in the device registry; "
            "set up local Modbus via discovery (or Add Integration) for a separate local entry",
            entry.title,
            host,
        )
        return

    from homeassistant.config_entries import SOURCE_IMPORT

    for serial, model in unique_serials:
        unique_id = f"modbus_{serial}"
        if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id) is not None:
            continue
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
                    CONF_SERIAL: serial,
                    CONF_MODEL: model,
                    CONF_MODBUS_HOST: host,
                    CONF_SCAN_INTERVAL: 30,
                    "modbus_debug_daily_yield": debug,
                },
            )
        )
        _LOGGER.info(
            "Created separate local Modbus entry for serial %s (host %s) split from cloud entry %s",
            serial,
            host,
            entry.title,
        )
