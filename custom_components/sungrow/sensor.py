from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_GATEWAY, DEFAULT_CONSOLE_URL, DOMAIN, GATEWAY_CONSOLE_URLS
from .coordinator import SungrowPlantCoordinator
from .measure_points import resolve_classification, resolve_enum_options, resolve_enum_value, resolve_name

# Sensors are read-only and updated in bulk by the coordinator, so no per-entity
# write parallelism limit is needed.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


def infer_device_class(
    unit: str | None, point_code: str, point_id: str = ""
) -> tuple[SensorDeviceClass | None, SensorStateClass | None]:
    """Infer a device and state class for a point (see ``measure_points``).

    Thin wrapper delegating to :func:`resolve_classification`; kept as a public
    name for the sensor platform and its tests. Returns ``(None, None)`` when the
    point cannot be classified so it is still created as a plain text sensor.
    """
    return resolve_classification(unit, point_code, point_id)


def _build_sensors(coordinator: SungrowPlantCoordinator, console_url: str) -> list[SungrowSensor]:
    """Build the full set of sensors the coordinator currently warrants.

    Called both at setup and on every coordinator update, so points/devices that
    appear after setup are surfaced at runtime (dynamic-devices). The caller
    de-duplicates by unique_id, so returning the complete set each time is fine.
    """
    sensors: list[SungrowSensor] = []

    # Plant-level sensors.
    # The data structure is { "P_CODE": { "code": ..., "value": ..., "unit": ..., "name": ... } }
    if coordinator.data:
        for point_code, point_data in coordinator.data.items():
            sensors.append(
                SungrowSensor(
                    coordinator, point_code, coordinator.plant_id, coordinator.plant_name, point_data, console_url
                )
            )

    # Per-device sensors (opt-in, issue #74): surface points reported per device
    # (EV chargers, meters, extra batteries). Points already present at the plant
    # level are skipped so enabling this doesn't just duplicate every plant sensor
    # under the inverter.
    if coordinator.enable_device_sensors and coordinator.device_data:
        plant_codes = set(coordinator.data or {})
        device_names = {
            str(d.get("uuid")): (d.get("device_name") or d.get("device_model_name") or coordinator.plant_name)
            for d in coordinator.devices
            if d.get("uuid")
        }
        for uuid, points in coordinator.device_data.items():
            device_name = device_names.get(uuid, f"Device {uuid}")
            for point_code, point_data in points.items():
                if point_code in plant_codes:
                    continue
                sensors.append(
                    SungrowDeviceSensor(coordinator, point_code, coordinator.plant_id, uuid, device_name, point_data)
                )

    return sensors


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow sensors from the coordinators created during entry setup."""
    coordinators = entry.runtime_data.coordinators
    # Point the device "Visit" link at the region's iSolarCloud web console.
    console_url = GATEWAY_CONSOLE_URLS.get(entry.data.get(CONF_GATEWAY, ""), DEFAULT_CONSOLE_URL)

    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        """Add entities for any plant points / devices not seen yet."""
        new_entities: list[SungrowSensor] = []
        for coordinator in coordinators:
            for entity in _build_sensors(coordinator, console_url):
                uid = entity.unique_id
                if uid is None or uid in known_unique_ids:
                    continue
                known_unique_ids.add(uid)
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    # Add the initial set, then keep watching each coordinator for new devices.
    _add_new_entities()
    for coordinator in coordinators:
        entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class SungrowSensor(CoordinatorEntity, SensorEntity):
    """Representation of a plant-level Sungrow sensor."""

    has_entity_name = True
    _point_id: str = ""

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        point_code: str,
        plant_id: str,
        plant_name: str,
        init_data: dict[str, Any],
        console_url: str = DEFAULT_CONSOLE_URL,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.point_code = point_code
        self.plant_id = plant_id
        self._attr_unique_id = f"{plant_id}_{point_code}"

        # Group sensors under a device per plant.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, plant_id)},
            name=plant_name,
            manufacturer="Sungrow",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=console_url,
        )
        self._apply_point_metadata(point_code, init_data, plant_name)

    def _apply_point_metadata(self, point_code: str, init_data: dict[str, Any], label: str) -> None:
        """Set the name, unit and device/state class from a point payload.

        Shared by plant-level and per-device sensors so both name and classify points
        the same way. The naming/classification logic lives in ``measure_points`` and
        is grounded in the official iSolarCloud catalogs.
        """
        # The numeric point ID (from the API payload) keys the documented catalog;
        # fall back to the code when the payload omits it.
        point_id = str(init_data.get("id") or point_code)
        self._point_id = point_id

        # Resolve a friendly English name (avoids the Chinese names the API often
        # returns even for English locales).
        self._attr_name = resolve_name(point_id, point_code, init_data.get("name"))
        _LOGGER.debug("Created sensor: %s %s (code: %s)", label, self._attr_name, point_code)

        # Hide points that are "Unknown" at first setup to reduce UI clutter
        # (e.g. meters/batteries not present).
        initial_value = init_data.get("value")
        if initial_value is None or str(initial_value).strip() == "" or str(initial_value).lower() == "unknown":
            self._attr_entity_registry_enabled_default = False

        # Infer device class / state class so the Energy dashboard and history graphs
        # work out of the box (issue #19), including the dimensionless points that
        # report no unit (SOC, SOH, power factor, PR, counts) (issue #105).
        unit = init_data.get("unit")
        device_class, state_class = resolve_classification(unit, point_code, point_id)
        self._attr_device_class = device_class
        self._attr_state_class = state_class

        if device_class == SensorDeviceClass.ENUM:
            # Enum sensors carry documented options and no unit/state class.
            self._attr_options = list(resolve_enum_options(point_id) or [])
            self._attr_native_unit_of_measurement = None
        else:
            self._attr_native_unit_of_measurement = unit if unit else None

        # Let HA choose the icon for sensors with a known device class; fall back to
        # the solar panel icon only for unclassified sensors.
        self._attr_icon = None if device_class else "mdi:solar-power-variant"

    def _current_point(self) -> dict[str, Any] | None:
        """Return the current point payload for this sensor (plant-level source)."""
        data = self.coordinator.data
        if data and self.point_code in data:
            point: dict[str, Any] = data[self.point_code]
            return point
        return None

    @property
    def native_value(self) -> float | str | None:
        """Return the state of the sensor."""
        point = self._current_point()
        if point is None:
            return None
        val: Any = point.get("value")
        if val is None:
            return None
        # Enum points map their raw numeric code to a documented human label.
        if self._attr_device_class == SensorDeviceClass.ENUM:
            return resolve_enum_value(self._point_id, val)
        # Only coerce to a number for sensors classified as numeric (a device or
        # state class). Unclassified text/status points are left as strings so a
        # status like "1" or a boolean isn't silently turned into a float.
        if self._attr_device_class is None and self._attr_state_class is None:
            return str(val)
        try:
            return float(val)
        except (ValueError, TypeError):
            # The sensor is classified numeric (a device or state class is set) but
            # the value can't be coerced (e.g. "unknown"). Return None rather than a
            # raw string, which HA would reject as an invalid state and which would
            # pollute long-term statistics (issue #113).
            return None


class SungrowDeviceSensor(SungrowSensor):
    """A sensor for a specific device (EV charger, meter, extra battery) under a plant.

    Reads from the coordinator's per-device realtime data and is grouped under its own
    device, linked to the plant device via ``via_device`` (issue #74).
    """

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        point_code: str,
        plant_id: str,
        device_uuid: str,
        device_name: str,
        init_data: dict[str, Any],
    ) -> None:
        """Initialize a device-scoped sensor."""
        # Skip SungrowSensor.__init__ (it builds the plant device); reuse the shared
        # metadata helper but with a device-scoped identity and data source.
        CoordinatorEntity.__init__(self, coordinator)
        self.point_code = point_code
        self.plant_id = plant_id
        self.device_uuid = device_uuid
        self._attr_unique_id = f"{plant_id}_{device_uuid}_{point_code}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            name=device_name,
            manufacturer="Sungrow",
            via_device=(DOMAIN, plant_id),
        )
        self._apply_point_metadata(point_code, init_data, device_name)

    def _current_point(self) -> dict[str, Any] | None:
        """Return the current point payload from the coordinator's per-device data."""
        device_data = getattr(self.coordinator, "device_data", None) or {}
        point: dict[str, Any] | None = device_data.get(self.device_uuid, {}).get(self.point_code)
        return point
