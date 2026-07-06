from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import build_device_info, build_plant_device_info, resolve_point_device
from .const import (
    BATTERY_DIAGNOSTIC_CODES,
    COMM_MODULE_POINTS,
    CONF_GATEWAY,
    DEFAULT_CONSOLE_URL,
    GATEWAY_CONSOLE_URLS,
    INVERTER_DIAGNOSTIC_POINTS,
)
from .coordinator import SungrowPlantCoordinator
from .measure_points import (
    PERCENT_FRACTION_POINT_IDS,
    resolve_classification,
    resolve_enum_options,
    resolve_enum_value,
    resolve_name,
)

# Sensors are read-only and updated in bulk by the coordinator, so no per-entity
# write parallelism limit is needed.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# Inverter + battery-health point codes get the DIAGNOSTIC entity category so they land
# in the device page's Diagnostic section instead of cluttering the main sensors (#149/#154).
_DIAGNOSTIC_CODES = (
    frozenset(INVERTER_DIAGNOSTIC_POINTS.values()) | BATTERY_DIAGNOSTIC_CODES | frozenset(COMM_MODULE_POINTS.values())
)

# Sentinel unit: tariff sensors take their unit from the plant's currency at runtime.
_CURRENCY_PER_KWH = "__currency_per_kwh__"

# Per-code icon overrides for points that read better with a specific icon than the
# generic solar-panel fallback (or the class default). Applies regardless of
# classification, so status/count points get a fitting glyph too.
_CODE_ICON_OVERRIDES = {
    "power_fraction": "mdi:gauge",
    "array_insulation_resistance": "mdi:omega",
    "afci_fault_count": "mdi:flash-alert",
    "battery_operation_status": "mdi:battery-sync",
    "battery_fault_module_id": "mdi:alert-circle",
}

# Dimensionless integer tallies displayed without a fractional part (issue-driven:
# a fault count of "3" reads better than "3.0").
_INTEGER_COUNT_CODES = frozenset({"afci_fault_count"})


@dataclass(frozen=True)
class PlantDetailSensor:
    """Descriptor for a plant-level sensor sourced from getPowerStationDetail (#178)."""

    key: str
    name: str
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    unit: str | None = None
    diagnostic: bool = True
    icon: str | None = None
    integer: bool = False


# Plant-detail fields worth surfacing as their own sensors on the plant device (#178):
# operational health (alarm/fault counts), the nameplate power, and the import/export
# tariffs the plant is configured with. Fields absent from a given plant are skipped.
PLANT_DETAIL_SENSORS: tuple[PlantDetailSensor, ...] = (
    PlantDetailSensor(
        "alarm_count",
        "Alarm Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-outline",
        integer=True,
    ),
    PlantDetailSensor(
        "fault_count",
        "Fault Count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:alert-circle-outline",
        integer=True,
    ),
    PlantDetailSensor(
        "install_power",
        "Installed Power",
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
        "W",
        icon="mdi:solar-power",
    ),
    # Import price is money paid (cash-minus); export price is money earned (cash-plus).
    PlantDetailSensor(
        "ps_consumption_power_price_kwh",
        "Import Price",
        unit=_CURRENCY_PER_KWH,
        diagnostic=False,
        icon="mdi:cash-minus",
    ),
    PlantDetailSensor(
        "ps_feedin_power_price_kwh", "Export Price", unit=_CURRENCY_PER_KWH, diagnostic=False, icon="mdi:cash-plus"
    ),
)


def infer_device_class(
    unit: str | None, point_code: str, point_id: str = ""
) -> tuple[SensorDeviceClass | None, SensorStateClass | None]:
    """Infer a device and state class for a point (see ``measure_points``).

    Thin wrapper delegating to :func:`resolve_classification`; kept as a public
    name for the sensor platform and its tests. Returns ``(None, None)`` when the
    point cannot be classified so it is still created as a plain text sensor.
    """
    return resolve_classification(unit, point_code, point_id)


def _build_sensors(coordinator: SungrowPlantCoordinator, console_url: str) -> list[SensorEntity]:
    """Build the full set of sensors the coordinator currently warrants.

    Called both at setup and on every coordinator update, so points/devices that
    appear after setup are surfaced at runtime (dynamic-devices). The caller
    de-duplicates by unique_id, so returning the complete set each time is fine.

    Points with no usable reading are skipped: the cloud returns the *full* measure-
    point catalogue regardless of installed hardware, so a PV-only plant still gets
    every battery/EMS/EV point back — as ``null`` or a ``"--"`` placeholder — which
    would otherwise litter the UI with permanently "Unknown" entities. Because this
    builder re-runs every poll, a point that later reports real data is added then.
    """
    sensors: list[SensorEntity] = []

    # Plant-level sensors.
    # The data structure is { "P_CODE": { "code": ..., "value": ..., "unit": ..., "name": ... } }
    if coordinator.data:
        for point_code, point_data in coordinator.data.items():
            sensor = SungrowSensor(
                coordinator, point_code, coordinator.plant_id, coordinator.plant_name, point_data, console_url
            )
            if sensor.native_value is None:
                continue
            sensors.append(sensor)

    # Plant-detail sensors (nameplate power, alarm/fault counts, import/export tariffs)
    # from getPowerStationDetail, attached to the plant device (#178). Fields the plant
    # doesn't report are skipped.
    for desc in PLANT_DETAIL_SENSORS:
        raw = coordinator.plant_detail.get(desc.key)
        if raw is None or str(raw).strip() == "":
            continue
        sensors.append(
            SungrowPlantDetailSensor(coordinator, desc, coordinator.plant_id, coordinator.plant_name, console_url)
        )

    # Per-device sensors (opt-in, issue #74): surface points reported per device
    # (EV chargers, meters, extra batteries). Points already present at the plant
    # level are skipped so enabling this doesn't just duplicate every plant sensor
    # under the inverter.
    if coordinator.enable_device_sensors and coordinator.device_data:
        plant_codes = set(coordinator.data or {})
        devices_by_uuid = {str(d["uuid"]): d for d in coordinator.devices if d.get("uuid")}
        for uuid, points in coordinator.device_data.items():
            device = devices_by_uuid.get(uuid, {"uuid": uuid})
            for point_code, point_data in points.items():
                if point_code in plant_codes:
                    continue
                sensor = SungrowDeviceSensor(coordinator, point_code, device, point_data)
                if sensor.native_value is None:
                    continue
                sensors.append(sensor)

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
        new_entities: list[SensorEntity] = []
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
    # Set for unit-less capacity-factor ratios (PERCENT_FRACTION_POINT_IDS): the raw
    # 0–1 fraction is presented as a percentage, so native_value scales it ×100.
    _scale_to_percent: bool = False

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

        # Re-home the sensor onto its physical device when the plant has exactly one
        # device of the mapped type; otherwise keep it on the plant device (#158). The
        # unique_id above is unchanged, so HA re-parents the entity without renaming it.
        device = resolve_point_device(point_code, getattr(coordinator, "devices", None) or [])
        if device is not None:
            self._attr_device_info = build_device_info(device, plant_id, fallback_name=plant_name)
        else:
            self._attr_device_info = build_plant_device_info(plant_id, plant_name, console_url)
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

        # Unit-less capacity-factor ratios (e.g. Plant Power / Installed Power) arrive
        # as a bare 0–1 fraction; present them as a percentage (see native_value).
        self._scale_to_percent = point_id in PERCENT_FRACTION_POINT_IDS

        if device_class == SensorDeviceClass.ENUM:
            # Enum sensors carry documented options and no unit/state class.
            self._attr_options = list(resolve_enum_options(point_id) or [])
            self._attr_native_unit_of_measurement = None
        elif self._scale_to_percent:
            self._attr_native_unit_of_measurement = PERCENTAGE
        elif device_class == SensorDeviceClass.SIGNAL_STRENGTH:
            # WLAN/wireless signal strength comes back with no unit; it's decibels.
            self._attr_native_unit_of_measurement = unit or "dB"
        else:
            self._attr_native_unit_of_measurement = unit if unit else None

        # Let HA pick the icon for classified sensors; a signal icon for signal
        # strength, a per-code override where one reads better, and the solar-panel
        # fallback only for otherwise-unclassified points.
        if device_class == SensorDeviceClass.SIGNAL_STRENGTH:
            self._attr_icon = "mdi:signal"
        elif point_code in _CODE_ICON_OVERRIDES:
            self._attr_icon = _CODE_ICON_OVERRIDES[point_code]
        else:
            self._attr_icon = None if device_class else "mdi:solar-power-variant"

        # Integer tallies (fault counts) show no decimal places.
        if point_code in _INTEGER_COUNT_CODES:
            self._attr_suggested_display_precision = 0

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
            num = float(val)
        except (ValueError, TypeError):
            # The sensor is classified numeric (a device or state class is set) but
            # the value can't be coerced (e.g. "unknown"). Return None rather than a
            # raw string, which HA would reject as an invalid state and which would
            # pollute long-term statistics (issue #113).
            return None
        # Capacity-factor ratios are reported as a 0–1 fraction but shown as "%".
        return num * 100 if self._scale_to_percent else num


class SungrowDeviceSensor(SungrowSensor):
    """A sensor for a specific device (EV charger, meter, extra battery) under a plant.

    Reads from the coordinator's per-device realtime data and is grouped under its own
    device, linked to the plant device via ``via_device`` (issue #74).
    """

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        point_code: str,
        device: dict[str, Any],
        init_data: dict[str, Any],
    ) -> None:
        """Initialize a device-scoped sensor."""
        # Skip SungrowSensor.__init__ (it builds the plant device); reuse the shared
        # metadata helper but with a device-scoped identity and data source.
        CoordinatorEntity.__init__(self, coordinator)
        self.point_code = point_code
        self.plant_id = coordinator.plant_id
        self.device_uuid = str(device["uuid"])
        device_name = device.get("device_name") or device.get("device_model_name") or coordinator.plant_name
        self._attr_unique_id = f"{self.plant_id}_{self.device_uuid}_{point_code}"
        # Enrich the device card with the model/serial the cloud reports.
        self._attr_device_info = build_device_info(device, self.plant_id, fallback_name=coordinator.plant_name)
        self._apply_point_metadata(point_code, init_data, device_name)
        # Inverter internals (operating status, MPPT, DC power, ...) are diagnostics.
        if point_code in _DIAGNOSTIC_CODES:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _current_point(self) -> dict[str, Any] | None:
        """Return the current point payload from the coordinator's per-device data."""
        device_data = getattr(self.coordinator, "device_data", None) or {}
        point: dict[str, Any] | None = device_data.get(self.device_uuid, {}).get(self.point_code)
        return point


class SungrowPlantDetailSensor(CoordinatorEntity[SungrowPlantCoordinator], SensorEntity):
    """A plant-level sensor sourced from getPowerStationDetail (#178).

    Surfaces operational health (alarm/fault counts), the nameplate power and the
    import/export tariffs on the plant device, refreshed from the coordinator's
    throttled plant-detail fetch rather than the realtime measure points.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        desc: PlantDetailSensor,
        plant_id: str,
        plant_name: str,
        console_url: str,
    ) -> None:
        """Initialize a plant-detail sensor."""
        super().__init__(coordinator)
        self._desc = desc
        self._attr_unique_id = f"{plant_id}_detail_{desc.key}"
        self._attr_name = desc.name
        self._attr_device_info = build_plant_device_info(plant_id, plant_name, console_url)
        self._attr_device_class = desc.device_class
        self._attr_state_class = desc.state_class
        self._attr_icon = desc.icon
        if desc.integer:
            self._attr_suggested_display_precision = 0
        if desc.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if desc.unit == _CURRENCY_PER_KWH:
            currency = str(coordinator.plant_detail.get("power_price_unit") or "").strip()
            self._attr_native_unit_of_measurement = f"{currency}/kWh" if currency else None
        else:
            self._attr_native_unit_of_measurement = desc.unit

    @property
    def native_value(self) -> float | int | str | None:
        """Return the current value of the plant-detail field."""
        raw = self.coordinator.plant_detail.get(self._desc.key)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return str(raw)
        # Counts (alarm/fault) are whole numbers, not floats.
        return int(num) if self._desc.integer else num
