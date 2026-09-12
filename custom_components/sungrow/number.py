"""Number entities for Sungrow iSolarCloud dispatch control."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, Literal

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from . import (
    DispatchControl,
    SungrowConfigEntry,
    build_device_info_for,
    select_dispatch_device,
    select_rating_fallbacks,
)
from .const import DOMAIN
from .coordinator import SungrowPlantCoordinator
from .entity_platform_helpers import create_entity_adder
from .modbus_control import ModbusControlError
from .model_specs import spec_for

_LOGGER = logging.getLogger(__name__)

# Dispatch writes go to a single device via one Control client; serialise them so
# rapid slider changes don't race on the API.
PARALLEL_UPDATES = 1

# Fallback upper bound (watts) for charge/discharge power, used when the device's
# rated power can't be derived from its model code.
DEFAULT_MAX_DISPATCH_POWER = 5000

# Sungrow residential inverters encode their kW rating in the model code, e.g.
# SG3.6RS -> 3.6 kW, SH10RT-V112 -> 10 kW, SG110CX -> 110 kW. Batteries, meters and
# comms modules (SBR256, SGSmartMeter, WiNet-S) don't match and fall back to the
# default. This is the only rating signal iSolarCloud exposes via getDeviceListByPsId.
_MODEL_POWER_RE = re.compile(r"S[GH](\d+(?:\.\d+)?)", re.IGNORECASE)


def rated_power_w(device: dict[str, Any]) -> int | None:
    """Best-effort rated power in watts parsed from a device's model code.

    Returns ``None`` when no rating can be parsed, so callers fall back to the
    default clamp.
    """
    match = _MODEL_POWER_RE.match(str(device.get("device_model_code") or ""))
    if not match:
        return None
    try:
        kw = float(match.group(1))
    except ValueError:
        return None
    if kw <= 0 or kw > 1000:  # guard against nonsense parses
        return None
    return int(round(kw * 1000))


# Number parameters exposed as HA Number entities. Keys are canonical Control
# parameter names. The entities present values in their natural unit (watts,
# percent); the raw value the API expects is produced by pysolarcloud's
# Control.encode_parameter (which knows, from the docs' Appendix 10, that power is
# watts, SOC/ratios are tenths of a percent, etc.) — so the encoding lives in one
# place instead of being duplicated here.
DISPATCH_NUMBERS: dict[str, dict[str, Any]] = {
    "charge_discharge_power": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        # Battery actuation: meaningless (and harmful — see #148) without a battery.
        "battery_only": True,
    },
    "soc_upper_limit": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 70,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        # SOC limits set battery policy rather than actuate — configuration entities.
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    "soc_lower_limit": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 50,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    "forced_charging_target_soc_1": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    # Target SOC for the second forced-charging window (mirrors ..._soc_1).
    "forced_charging_target_soc_2": {
        "device_class": NumberDeviceClass.BATTERY,
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    # Export (feed-in) limit as an absolute power in watts. Only takes effect when
    # the feed_in_limitation select is enabled. Sized to the device's rating.
    "feed_in_limitation_value": {
        "device_class": NumberDeviceClass.POWER,
        "native_unit_of_measurement": "W",
        "native_min_value": 0,
        "native_max_value": DEFAULT_MAX_DISPATCH_POWER,
        "native_step": 100,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Export limit as a percentage of rated power (API range 0-1000 = 0-100%).
    "feed_in_limitation_ratio": {
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Active power output cap as a percentage of rated power (API range 0-1000).
    "active_power_limit_ratio": {
        "native_unit_of_measurement": "%",
        "native_min_value": 0,
        "native_max_value": 100,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Reactive power ratio Q(t) as a signed percentage (API range -600..600 = -60..60%).
    # Only takes effect when the Reactive Power Mode select is set to Q(t). Applies to
    # PV and hybrid inverters, so not battery-gated.
    "q_t": {
        "native_unit_of_measurement": "%",
        "native_min_value": -60,
        "native_max_value": 60,
        "native_step": 1,
        "mode": NumberMode.SLIDER,
        "entity_category": EntityCategory.CONFIG,
    },
    # Power factor setpoint (API range -1000..1000 = -1..1). Only takes effect when the
    # Reactive Power Mode select is set to PF.
    "pf": {
        "device_class": NumberDeviceClass.POWER_FACTOR,
        "native_min_value": -1,
        "native_max_value": 1,
        "native_step": 0.01,
        "mode": NumberMode.BOX,
        "entity_category": EntityCategory.CONFIG,
    },
}

# Watt-valued dispatch parameters whose slider maximum is sized from the device.
# The value picks the resolution strategy — ``"ac"`` reads the AC nameplate
# (``feed_in_limitation_value``, since an export limit cannot exceed what the
# inverter can push to the grid), ``"battery"`` reads the battery-side rating
# (``charge_discharge_power``, since SH-RS hybrids drive 6.6–10.6 kW to the
# battery from a 3–6 kW AC output — the battery limit exceeds the AC rating).
#
# Adding a new watt-valued parameter is a one-line change here; ``_resolve_param_max_power``
# routes on the value. Non-watt params (percent, duration, ratio) don't appear
# in this mapping — their bounds are static per :data:`DISPATCH_NUMBERS`.
_POWER_PARAM_RATING_KIND: dict[str, Literal["ac", "battery"]] = {
    "feed_in_limitation_value": "ac",
    "charge_discharge_power": "battery",
}


def _device_ac_rating(device: dict[str, Any]) -> int | None:
    """Return a device's AC-side nameplate in watts, or ``None`` when unknown.

    Datasheet catalog first — the authoritative per-model number where known, and more
    precise than the model code (SG3.6RS parses to 3600 W but the datasheet lists
    3680 W) — then the model-code regex for models outside the catalog.
    """
    spec = spec_for(str(device.get("device_model_code") or ""))
    if spec is not None:
        return spec.max_ac_output_power
    return rated_power_w(device)


def _device_battery_rating(device: dict[str, Any]) -> int | None:
    """Return a device's battery-side power limit in watts, or ``None`` when unknown.

    Battery-side sliders drive charge OR discharge, so the ceiling is
    ``max(charge_power, discharge_power)`` from the datasheet. Rows flagged
    ``unverified=True`` in the catalog (#349) are treated conservatively: their battery
    values are TCzerny family estimates, not datasheet lookups, so this ignores them and
    falls back to the AC-side rating. That keeps the slider ceiling at or below the
    inverter's nameplate — never overshooting into an estimated-battery value that could
    exceed real hardware. Models with no battery data fall back the same way.
    """
    spec = spec_for(str(device.get("device_model_code") or ""))
    if spec is not None and not spec.unverified:
        battery_limits = [x for x in (spec.max_charge_power, spec.max_discharge_power) if x is not None]
        if battery_limits:
            return max(battery_limits)
    return _device_ac_rating(device)


def _resolve_ac_rated_power(target: dict[str, Any], fallbacks: Sequence[dict[str, Any]] = ()) -> int:
    """Return the device's AC-side rated output power in watts.

    Single source of truth for AC-side rating resolution, priority-ordered:

    1. Datasheet catalog (:func:`~custom_components.sungrow.model_specs.spec_for`) —
       the authoritative per-model number where known. Preferred over the regex
       fallback because e.g. SG3.6RS parses to 3600 W but the datasheet lists 3680 W.
    2. Model-code regex (:func:`rated_power_w`) — catches unknown models that
       still encode the kW rating in their model code.
    3. Each ``fallbacks`` device in turn — see :func:`_resolve_battery_rated_power`.
    4. :data:`DEFAULT_MAX_DISPATCH_POWER` — final conservative clamp.
    """
    for device in (target, *fallbacks):
        rating = _device_ac_rating(device)
        if rating is not None:
            return rating
    return DEFAULT_MAX_DISPATCH_POWER


def _resolve_battery_rated_power(target: dict[str, Any], fallbacks: Sequence[dict[str, Any]] = ()) -> int:
    """Return the device's battery-side rated power (max of charge/discharge) in watts.

    Falls back to the AC rating for models without battery entries in the catalog, and
    finally to :data:`DEFAULT_MAX_DISPATCH_POWER` — same conservative floor as
    :func:`_resolve_ac_rated_power`.

    ``fallbacks`` are the plant's other inverters/energy-storage systems, tried in order
    when the write target's own model code resolves no rating at all. iSolarCloud
    sometimes labels a hybrid's ESS entry with the battery model code, which used to
    clamp the slider to the default below what the hardware supports (#422).
    """
    for device in (target, *fallbacks):
        rating = _device_battery_rating(device)
        if rating is not None:
            return rating
    return DEFAULT_MAX_DISPATCH_POWER


def _resolve_param_max_power(
    param: str, target: dict[str, Any], fallbacks: Sequence[dict[str, Any]] = ()
) -> int | None:
    """Return the slider ceiling for a watt-valued dispatch parameter, or ``None``.

    Consults :data:`_POWER_PARAM_RATING_KIND` to decide whether the parameter is
    AC-side or battery-side, and routes to the appropriate resolver. Returns
    ``None`` for parameters that aren't watt-valued (their bounds are static).
    """
    kind = _POWER_PARAM_RATING_KIND.get(param)
    if kind == "ac":
        return _resolve_ac_rated_power(target, fallbacks)
    if kind == "battery":
        return _resolve_battery_rated_power(target, fallbacks)
    return None


def _resolve_param_rating(param: str, target: dict[str, Any], fallbacks: Sequence[dict[str, Any]] = ()) -> int | None:
    """Return the rating a watt-valued parameter resolves to, or ``None`` when none exists.

    :func:`_resolve_param_max_power` cannot answer this: it returns
    :data:`DEFAULT_MAX_DISPATCH_POWER` both when a device genuinely is that size (SG5.0RS is
    legitimately 5000 W) and when nothing could be resolved at all. The Repair must fire only
    for the second case, so the per-device helpers are consulted directly here.
    """
    kind = _POWER_PARAM_RATING_KIND.get(param)
    if kind is None:
        return None
    for device in (target, *fallbacks):
        rating = _device_ac_rating(device) if kind == "ac" else _device_battery_rating(device)
        if rating is not None:
            return rating
    return None


RATING_UNKNOWN_ISSUE = "dispatch_rating_unknown"


def _async_manage_rating_repair(
    hass: HomeAssistant, coordinator: SungrowPlantCoordinator, target: dict[str, Any], unresolved: Sequence[str]
) -> None:
    """Raise or clear the Repair for sliders capped by an unknown nameplate (#429).

    Without it the only symptom is a slider that stops in the wrong place, which is exactly
    how #422 was reported, and every such report costs a round-trip to discover that the fix
    is a one-line addition to the model catalog.
    """
    issue_id = f"{RATING_UNKNOWN_ISSUE}_{coordinator.plant_id}"
    if not unresolved:
        # A rating resolved, so the ceiling is no longer a guess and the Repair is stale.
        # This runs on every coordinator update, which is what makes adding the model to the
        # catalog clear the issue on the next poll.
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=RATING_UNKNOWN_ISSUE,
        translation_placeholders={
            "plant": coordinator.plant_name,
            "model": str(target.get("device_model_code") or "unknown"),
            "ceiling": str(DEFAULT_MAX_DISPATCH_POWER),
            "parameters": ", ".join(sorted(unresolved)),
        },
    )


def _build_numbers(
    coordinator: SungrowPlantCoordinator, control: DispatchControl | None, hass: HomeAssistant
) -> list[NumberEntity]:
    """Build the dispatch number entities for a coordinator's target device.

    Returns an empty list when no dispatch-capable device is present. Reads the
    coordinator's live device list so a dispatchable device that appears after
    setup gets its controls at runtime (dynamic-devices).
    """
    if control is None:
        return []
    # Skip entirely if the device reported that it doesn't accept parameter writes.
    if not coordinator.dispatch_update_supported:
        return []
    # Prefer the ESS device if present, otherwise fall back to an inverter.
    target = select_dispatch_device(coordinator.devices)
    if target is None:
        return []
    if not target.get("uuid"):
        return []
    # A hybrid's ESS entry is sometimes labelled with the battery model code, which
    # resolves no power rating at all; the plant's other inverters/ESS devices carry the
    # real nameplate, so offer them as fallback rating sources (#422). The write target
    # itself never changes — only the slider ceiling.
    rating_fallbacks = select_rating_fallbacks(target, coordinator.devices)
    # Local ModbusControl only maps a subset of Appendix-10 params (#220).
    # Require a real collection so MagicMock control clients in tests are unaffected.
    raw_supported = getattr(control, "supported_parameters", None)
    supported = raw_supported if isinstance(raw_supported, (set, frozenset)) else None
    entities: list[NumberEntity] = []
    # Watt-valued params whose ceiling had to fall back to the conservative default because
    # no nameplate resolved anywhere on the plant (#429). Collected so the Repair can be
    # raised (or cleared) once, after the whole control set is known.
    unresolved_ratings: list[str] = []
    for param, meta in DISPATCH_NUMBERS.items():
        # Hide battery-only controls on PV-only plants — see #148.
        if meta.get("battery_only") and not coordinator.has_battery:
            continue
        if supported is not None and param not in supported:
            continue
        # Watt-valued params get their slider ceiling from the device's rated power
        # (:func:`_resolve_param_max_power` returns ``None`` for non-watt params).
        if param in _POWER_PARAM_RATING_KIND and _resolve_param_rating(param, target, rating_fallbacks) is None:
            unresolved_ratings.append(param)
        max_power = _resolve_param_max_power(param, target, rating_fallbacks)
        if max_power is not None and max_power != meta["native_max_value"]:
            meta = {**meta, "native_max_value": max_power}
        entities.append(SungrowDispatchNumber(coordinator, control, target, param, meta))
    _async_manage_rating_repair(hass, coordinator, target, unresolved_ratings)
    # The forced-dispatch auto-revert timeout only makes sense alongside the battery
    # charge/discharge controls, so gate it on the same has_battery check (#157/#148).
    if coordinator.has_battery:
        entities.append(SungrowForcedDispatchDurationNumber(coordinator, target))
    return entities


async def async_setup_entry(
    hass: HomeAssistant, entry: SungrowConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Sungrow dispatch number entities."""
    data = entry.runtime_data
    control = data.control
    coordinators = data.coordinators

    adder = create_entity_adder(
        hass,
        entry,
        "number",
        coordinators,
        lambda coordinator: _build_numbers(coordinator, control, hass),
        async_add_entities,
    )
    adder()
    for coordinator in coordinators:
        entry.async_on_unload(coordinator.async_add_listener(adder))


class SungrowDispatchNumber(CoordinatorEntity[SungrowPlantCoordinator], RestoreNumber):
    """Number entity for a Sungrow dispatch parameter."""

    _attr_has_entity_name = True
    # Dispatch parameters are write-only: the API doesn't read the current setpoint back
    # (getDevPropertyPointValue is permission-gated), so the value shown is the last one
    # we commanded — an assumption, not a device reading. Unset until first set/restored,
    # which correctly reads as "unknown" per HA's entity-unavailable guidance.
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        control: DispatchControl,
        device: dict[str, Any],
        param: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialize the dispatch number."""
        super().__init__(coordinator)
        self.control = control
        self.device_uuid = str(device["uuid"])
        self.param = param
        # Entity name comes from translations (entity.number.<param>.name).
        self._attr_translation_key = param
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_{param}"
        self._attr_device_info = build_device_info_for(coordinator, device)
        self._attr_device_class = meta.get("device_class")
        self._attr_native_unit_of_measurement = meta.get("native_unit_of_measurement")
        self._attr_native_min_value = meta["native_min_value"]
        self._attr_native_max_value = meta["native_max_value"]
        self._attr_native_step = meta["native_step"]
        self._attr_mode = meta["mode"]
        self._attr_entity_category = meta.get("entity_category")

    async def async_added_to_hass(self) -> None:
        """Restore the last commanded value across restarts.

        Dispatch parameters are write-only (not polled back from the API), so the
        last value the user set is restored from state rather than fetched.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = self._clamp_to_bounds(last.native_value)

    def _clamp_to_bounds(self, value: float) -> float:
        """Clamp a restored value to the current slider bounds.

        Those bounds are not constant: the datasheet catalog added battery-side limits
        (#332) and the ceiling can now also be resolved from a sibling device (#422), so a
        value restored from an earlier build can sit outside today's range. Home Assistant
        validates *service calls* against min/max, not restored state, so without this the
        UI would show an impossible setpoint — and any automation reading it would
        inherit the value (#425).
        """
        clamped = min(max(value, self._attr_native_min_value), self._attr_native_max_value)
        if clamped != value:
            _LOGGER.debug(
                "Clamping restored %s for %s from %s to %s; the bounds changed since it was set",
                self.param,
                self.device_uuid,
                value,
                clamped,
            )
        return clamped

    async def async_set_native_value(self, value: float) -> None:
        """Update the dispatch parameter on the inverter."""
        _LOGGER.debug("Setting %s to %s for %s", self.param, value, self.device_uuid)
        # Encode the displayed value into the raw value the API expects (watts,
        # tenths-of-a-percent, etc.) using pysolarcloud's authoritative specs.
        wire_value = Control.encode_parameter(self.param, value)
        try:
            await self.control.async_update_parameters(self.device_uuid, {self.param: wire_value})
        except (PySolarCloudException, ModbusControlError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dispatch_write_failed",
                translation_placeholders={"param": self.param, "error": str(err)},
            ) from err
        # Remember the commanded value so the UI reflects it and it survives restarts.
        # Writing power only sets the target: the EMS heartbeat is owned solely by the
        # command select (Charge/Discharge start it, Stop stops it), so writing power
        # — even 0 — never arms or re-arms dispatch here (see #112).
        self._attr_native_value = value
        # Nothing polls a dispatch parameter back (it is write-only), so without this the
        # slider would snap back to the previous value until the next coordinator poll.
        self.async_write_ha_state()


# Default duration (minutes) for a forced Charge/Discharge before auto-revert (#157 / #255).
# A non-zero default means forced commands always have a bounded lifetime out of the box;
# users can still set 0 to opt out of auto-revert.
DEFAULT_FORCED_DISPATCH_DURATION = 60


class SungrowForcedDispatchDurationNumber(CoordinatorEntity[SungrowPlantCoordinator], RestoreNumber):
    """Local number controlling the forced-dispatch auto-revert timeout (#157 / #255).

    Unlike the other dispatch numbers this writes *nothing* to the inverter — it only
    records, on the coordinator, how long a forced Charge/Discharge command may stay
    active before the command select reverts it to Stop. 0 disables auto-revert.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "forced_dispatch_duration"
    _attr_native_min_value = 0
    _attr_native_max_value = 1440  # 24 h
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SungrowPlantCoordinator, device: dict[str, Any]) -> None:
        """Initialize the forced-dispatch duration number."""
        super().__init__(coordinator)
        self.device_uuid = str(device["uuid"])
        # Identifies the entity (like the dispatch numbers) though it writes no param.
        self.param = "forced_dispatch_duration"
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_forced_dispatch_duration"
        # Goes through the shared helper so the parent link is never pointed at the
        # unregistered inverter serial on local Modbus entries (#383).
        self._attr_device_info = build_device_info_for(coordinator, device)
        self._attr_native_value = DEFAULT_FORCED_DISPATCH_DURATION
        # NB: the coordinator is deliberately *not* seeded here. The entity adder
        # rebuilds every number on each coordinator update, so a write in __init__
        # would reset a user-configured duration back to the default on every poll.
        # The authoritative value is published in async_added_to_hass /
        # async_set_native_value below.

    async def async_added_to_hass(self) -> None:
        """Restore the configured duration and publish it to the coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value
        self.coordinator.forced_dispatch_duration_minutes = self._attr_native_value or 0

    async def async_set_native_value(self, value: float) -> None:
        """Store the new duration locally and publish it to the coordinator."""
        self._attr_native_value = value
        self.coordinator.forced_dispatch_duration_minutes = value
        # This entity is rebuilt on every coordinator update, and nothing reads the
        # duration back from the device, so push the new value now (#157).
        self.async_write_ha_state()
