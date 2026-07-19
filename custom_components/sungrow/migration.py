"""Config-entry version migration and legacy hybrid entry splitting.

Extracted from ``__init__.py`` (#289). ``async_migrate_entry`` is re-exported
from the package root so Home Assistant can discover it.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

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

# Legacy select unique_id suffix retired in v5.0.0 (replaced by ``_battery_mode``, #255).
# HA does not auto-remove renamed entities, so the old registry row lingers as
# "unavailable" until we sweep it (issue #314).
_LEGACY_SELECT_SUFFIXES: tuple[str, ...] = ("_charge_discharge_command",)


def _remove_legacy_entities(hass: HomeAssistant, config_entry: ConfigEntry) -> int:
    """Purge entity-registry rows renamed away in earlier releases.

    Idempotent: entries already missing from the registry are skipped, so calling this
    on every migration or setup is safe. Returns the number of entities removed for
    logging/testing.
    """
    registry = er.async_get(hass)
    removed = 0
    for entity in list(er.async_entries_for_config_entry(registry, config_entry.entry_id)):
        if entity.platform != DOMAIN:
            continue
        unique_id = entity.unique_id or ""
        if not any(unique_id.endswith(suffix) for suffix in _LEGACY_SELECT_SUFFIXES):
            continue
        registry.async_remove(entity.entity_id)
        removed += 1
        _LOGGER.info(
            "Removed legacy Sungrow entity %s (unique_id=%s); superseded by select.*_battery_mode",
            entity.entity_id,
            unique_id,
        )
    return removed


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

    if config_entry.version == 3:
        # v3→v4: sweep entities renamed away in v5.0.0 (issue #314). The
        # ``select.*_charge_discharge_command`` unique_id was replaced by
        # ``select.*_battery_mode``; HA leaves the old row registered but permanently
        # unavailable until we remove it here.
        removed = _remove_legacy_entities(hass, config_entry)
        hass.config_entries.async_update_entry(config_entry, version=4)
        _LOGGER.info(
            "Migrated config entry %s to version 4 (removed %d legacy entities)",
            config_entry.title,
            removed,
        )

    if config_entry.version == 4:
        # v4→v5: retire the ``cloud_modbus`` transport (#348). The transport was
        # selectable in the config flow but ``async_setup_entry`` never wired the
        # Modbus side (the deferred #217 was closed as ``not_planned``), so existing
        # entries loaded to zero entities. Convert them to ``cloud_only`` — dropping
        # the now-unused ``modbus_host`` — so the cloud coordinator takes over and
        # users get working entities on the next reload.
        new_data = dict(config_entry.data)
        if new_data.get(CONF_TRANSPORT) == TRANSPORT_CLOUD_MODBUS:
            new_data[CONF_TRANSPORT] = TRANSPORT_CLOUD_ONLY
            dropped_host = new_data.pop(CONF_MODBUS_HOST, None)
            _LOGGER.warning(
                "Migrated config entry %s from cloud_modbus to cloud_only (#348); "
                "the local Modbus side was never wired. Dropped modbus_host=%s. "
                "Set up local Modbus via a separate 'Modbus Only' entry if you need it.",
                config_entry.title,
                dropped_host,
            )
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=5)

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
    # ``cloud_modbus`` was retired in #348; the v4→v5 migration above converts those
    # entries to ``cloud_only`` (dropping ``modbus_host``) before this runs, so the
    # legacy-hybrid split now only fires on genuinely-legacy hybrid options-shape
    # entries where a stale ``modbus_host`` still lingers in options.
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
