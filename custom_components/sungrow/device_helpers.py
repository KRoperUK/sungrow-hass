"""Device-registry helper functions for the Sungrow integration.

Extracted from ``__init__.py`` (#289) to keep the entry setup module focused on
lifecycle logic. All public names are re-exported from ``__init__`` for backward
compatibility.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from pysolarcloud.plants import DeviceType

from .const import CONF_TRANSPORT, DOMAIN, POINT_DEVICE_TYPE, TRANSPORT_MODBUS_ONLY


def _matches_device_type(device: dict[str, Any], target: DeviceType) -> bool:
    """Return True if a discovered device is of ``target`` type.

    pysolarcloud converts a *known* device type to a ``DeviceType`` enum, but the
    raw API (and test mocks) may present it as an int or a string, so match against
    all three representations rather than a single one.
    """
    dt = device.get("device_type")
    return dt in (target, target.value, target.name)


def select_dispatch_device(devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the device to attach dispatch (number/select) entities to.

    Only inverters and energy-storage systems accept charge/discharge dispatch, so
    ignore any other discovered devices (meters, EV chargers, ...). Prefer an ESS,
    then fall back to an inverter. Returns ``None`` when neither is present.
    """
    ess = [d for d in devices if _matches_device_type(d, DeviceType.ENERGY_STORAGE_SYSTEM)]
    if ess:
        return ess[0]
    inverters = [d for d in devices if _matches_device_type(d, DeviceType.INVERTER)]
    return inverters[0] if inverters else None


def resolve_point_device(point_code: str, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single physical device a plant point belongs to, else None (=plant).

    Re-homes a flat plant sensor onto its device (#158) only when the plant has exactly
    one device of a mapped type (the "singular" rule); 0 or >1 matches keep the point on
    the plant device so genuine aggregates (e.g. total power on a 2-inverter plant) stay
    correct. Codes with no mapping also stay on the plant.
    """
    types = POINT_DEVICE_TYPE.get(point_code)
    if not types:
        return None
    matches = [d for d in devices if d.get("uuid") and any(_matches_device_type(d, t) for t in types)]
    return matches[0] if len(matches) == 1 else None


# Sentinel: omit ``via_plant_id`` to nest under ``plant_id`` (cloud default). Pass
# ``via_plant_id=None`` explicitly for a local Modbus inverter with no cloud plant so
# we do **not** invent a non-existent parent (HA 2025.12 warns / breaks).
_VIA_PLANT_UNSET: object = object()


def build_device_info(
    device: dict[str, Any],
    plant_id: str,
    *,
    fallback_name: str | None = None,
    via_plant_id: str | None | object = _VIA_PLANT_UNSET,
    configuration_url: str | None = None,
) -> DeviceInfo:
    """Build a device-registry entry for a physical device, nested under its plant.

    Enriches the HA device card with the model, serial number and manufacturer the
    cloud reports (``device_model_code`` / ``device_sn`` / ``factory_name`` from
    ``getDeviceListByPsId``) instead of a bare name, and links it to the plant device
    via ``via_device``. The uuid is stringified so the identifier matches
    ``_known_device_ids`` (which keys on ``str(uuid)``) and the device isn't pruned.

    ``via_plant_id`` overrides the parent plant identifier (local Modbus nesting under a
    matching cloud plant). Pass ``None`` to leave the device un-nested when no plant
    parent exists yet — never point ``via_device`` at a missing identifier.
    """
    if via_plant_id is _VIA_PLANT_UNSET:
        parent_id: str | None = plant_id
    else:
        parent_id = via_plant_id  # type: ignore[assignment]
    info = DeviceInfo(
        identifiers={(DOMAIN, str(device["uuid"]))},
        name=device.get("device_name") or device.get("device_model_name") or fallback_name,
        manufacturer=device.get("factory_name") or "Sungrow",
        model=device.get("device_model_code") or device.get("device_model_name"),
        serial_number=device.get("device_sn"),
    )
    if parent_id is not None:
        info["via_device"] = (DOMAIN, parent_id)
    if configuration_url:
        info["configuration_url"] = configuration_url
    return info


def build_plant_device_info(plant_id: str, plant_name: str, console_url: str) -> DeviceInfo:
    """Build the plant "service" DeviceInfo that anchors the per-device ``via_device`` tree.

    Registered explicitly at setup and used as the fallback for any plant sensor that does
    not re-home onto a physical device (#158), so the plant device always exists as the
    parent even when every sensor moves onto an inverter/battery/meter.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, plant_id)},
        name=plant_name,
        manufacturer="Sungrow",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=console_url,
    )


def find_related_cloud_plant_id(hass: HomeAssistant, serial: str) -> str | None:
    """Return the cloud plant identifier that already owns this inverter serial, if any.

    Used so a separate Modbus-only entry can nest its local inverter under the cloud
    plant device without merging sensor values.
    """
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_TRANSPORT) == TRANSPORT_MODBUS_ONLY:
            continue
        inv = None
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if device.serial_number and device.serial_number == serial:
                inv = device
                break
        if inv is None:
            continue
        if inv.via_device_id:
            parent = registry.async_get(inv.via_device_id)
            if parent is not None:
                for domain_key, ident in parent.identifiers:
                    if domain_key == DOMAIN:
                        return str(ident)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if device.entry_type == dr.DeviceEntryType.SERVICE:
                for domain_key, ident in device.identifiers:
                    if domain_key == DOMAIN:
                        return str(ident)
    return None
