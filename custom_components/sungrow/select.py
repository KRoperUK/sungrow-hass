"""Select entities for Sungrow iSolarCloud dispatch control."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pysolarcloud import PySolarCloudException
from pysolarcloud.control import Control

from . import async_start_heartbeat, async_stop_heartbeat, build_device_info, select_dispatch_device
from .const import DOMAIN
from .coordinator import SungrowPlantCoordinator

_LOGGER = logging.getLogger(__name__)

# Dispatch writes go to a single device via one Control client; serialise them.
PARALLEL_UPDATES = 1

# Select parameters exposed as HA Select entities.
DISPATCH_SELECTS: dict[str, dict[str, Any]] = {
    "charge_discharge_command": {
        "options_map": {
            "Stop": Control.CHARGE_DISCHARGE_COMMANDS["stop"],
            "Charge": Control.CHARGE_DISCHARGE_COMMANDS["charge"],
            "Discharge": Control.CHARGE_DISCHARGE_COMMANDS["discharge"],
        },
        # Battery actuation: meaningless (and harmful — see #148) without a battery.
        "battery_only": True,
    },
    "forced_charging": {
        "options_map": {
            "Disable": Control.FORCED_CHARGING["disable"],
            "Enable": Control.FORCED_CHARGING["enable"],
        },
        # Enabling/disabling forced charging is a policy setting, not actuation.
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    # Sungrow enable/disable enum (device-verified): Enable=170, Disable=85.
    "feed_in_limitation": {
        "options_map": {"Disable": "85", "Enable": "170"},
        "entity_category": EntityCategory.CONFIG,
    },
    "limited_power_switch": {
        "options_map": {"Disable": "85", "Enable": "170"},
        "entity_category": EntityCategory.CONFIG,
    },
    # Battery-first mode (hybrid inverters only; a graceful write error is raised
    # on devices that don't support it).
    "battery_first": {
        "options_map": {"Disable": "85", "Enable": "170"},
        "entity_category": EntityCategory.CONFIG,
        "battery_only": True,
    },
    # Reactive power regulation mode (Appendix 10, param 10009). Gates the Q(t) and Power
    # Factor numbers. Applies to PV and hybrid inverters, so not battery-gated.
    "reactive_power_regulation_mode": {
        "options_map": {
            "Off": "85",
            "Power Factor": "161",
            "Reactive Power Ratio Q(t)": "162",
            "Q(P) Curve": "163",
            "Q(U) Curve": "164",
        },
        "entity_category": EntityCategory.CONFIG,
    },
}


def _build_selects(coordinator: SungrowPlantCoordinator, control: Control) -> list[SelectEntity]:
    """Build the dispatch select entities for a coordinator's target device.

    Returns an empty list when no dispatch-capable device is present. Reads the
    coordinator's live device list so a dispatchable device that appears after
    setup gets its controls at runtime (dynamic-devices).
    """
    # Skip entirely if the device reported that it doesn't accept parameter writes.
    if not coordinator.dispatch_update_supported:
        return []
    # Prefer the ESS device if present, otherwise fall back to an inverter.
    target = select_dispatch_device(coordinator.devices)
    if target is None:
        return []
    if not target.get("uuid"):
        return []
    # Hide battery-only controls on PV-only plants — see #148.
    return [
        SungrowDispatchSelect(coordinator, control, target, param, meta)
        for param, meta in DISPATCH_SELECTS.items()
        if coordinator.has_battery or not meta.get("battery_only")
    ]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up Sungrow dispatch select entities."""
    data = entry.runtime_data
    control = data.control
    coordinators = data.coordinators

    known_unique_ids: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        new_entities: list[SelectEntity] = []
        for coordinator in coordinators:
            for entity in _build_selects(coordinator, control):
                uid = entity.unique_id
                if uid is None or uid in known_unique_ids:
                    continue
                known_unique_ids.add(uid)
                new_entities.append(entity)
        if new_entities:
            async_add_entities(new_entities)

    _add_new_entities()
    for coordinator in coordinators:
        entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class SungrowDispatchSelect(CoordinatorEntity[SungrowPlantCoordinator], RestoreEntity, SelectEntity):
    """Select entity for a Sungrow dispatch parameter."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SungrowPlantCoordinator,
        control: Control,
        device: dict[str, Any],
        param: str,
        meta: dict[str, Any],
    ) -> None:
        """Initialize the dispatch select."""
        super().__init__(coordinator)
        self.control = control
        self.device_uuid = str(device["uuid"])
        self.param = param
        self.options_map = dict(meta["options_map"])
        reverse_map = {v: k for k, v in self.options_map.items()}
        self._reverse_map = reverse_map
        # Entity name comes from translations (entity.select.<param>.name).
        self._attr_translation_key = param
        self._attr_unique_id = f"{coordinator.plant_id}_{self.device_uuid}_{param}"
        # Nest the dispatch device under the plant device the sensors created,
        # enriched with the model/serial the cloud reports.
        self._attr_device_info = build_device_info(device, coordinator.plant_id, fallback_name=coordinator.plant_name)
        self._attr_options = list(self.options_map.keys())
        self._attr_entity_category = meta.get("entity_category")
        # Auto-revert timer for forced Charge/Discharge (#157). Only the command select
        # uses these; other selects leave them unset.
        self._revert_cancel: CALLBACK_TYPE | None = None
        self._revert_deadline: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last selected option across restarts.

        Dispatch commands are write-only (not polled back from the API), so the
        last option the user chose is restored from state rather than fetched.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self.options_map:
            self._attr_current_option = last.state
            # If we were mid-charge/discharge before the restart, resume the EMS
            # heartbeat: restoring current_option alone would leave the UI showing
            # Charge/Discharge while the inverter silently times out of External-EMS
            # mode. Only the command select owns the heartbeat, so this restarts it
            # exactly once per plant; async_start_heartbeat is idempotent (it stops
            # any existing loop first), so a stale loop is never double-started (#112).
            if self.param == "charge_discharge_command" and last.state in {"Charge", "Discharge"}:
                entry = self.coordinator.config_entry
                assert entry is not None
                # Restore the auto-revert deadline first (#157): if the forced command
                # already timed out while HA was down, revert to Stop instead of resuming
                # dispatch; otherwise resume the heartbeat and re-arm for the time left.
                deadline = self._restore_revert_deadline(last.attributes.get("revert_at"))
                if deadline is not None and deadline <= time.time():
                    await self._do_revert()
                    return
                await async_start_heartbeat(
                    self.hass,
                    entry,
                    self.coordinator.plant_id,
                    self.device_uuid,
                    interval=60,
                )
                if deadline is not None:
                    self._arm_revert(deadline=deadline)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the auto-revert timer when the command select is removed."""
        self._cancel_revert()
        await super().async_will_remove_from_hass()

    @staticmethod
    def _restore_revert_deadline(raw: Any) -> float | None:
        """Parse a restored ``revert_at`` epoch attribute, or None if absent/invalid."""
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def async_select_option(self, option: str) -> None:
        """Update the dispatch parameter on the inverter."""
        value = self.options_map[option]
        _LOGGER.debug("Setting %s to %s (%s) for %s", self.param, option, value, self.device_uuid)
        try:
            await self.control.async_update_parameters(self.device_uuid, {self.param: value})
        except PySolarCloudException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dispatch_write_failed",
                translation_placeholders={"param": self.param, "error": str(err)},
            ) from err
        # Remember the selected option so the UI reflects it and it survives restarts.
        self._attr_current_option = option

        # Keep the inverter in dispatch mode while actively charging/discharging.
        entry = self.coordinator.config_entry
        assert entry is not None
        if self.param == "charge_discharge_command" and option in {"Charge", "Discharge"}:
            await async_start_heartbeat(
                self.hass,
                entry,
                self.coordinator.plant_id,
                self.device_uuid,
                interval=60,
            )
            # Arm the auto-revert so this forced command can't silently persist (#157).
            self._arm_revert()
        elif self.param == "charge_discharge_command" and option == "Stop":
            await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
            self._cancel_revert()

    # NOTE: the heartbeat is deliberately NOT stopped from async_will_remove_from_hass.
    # All ~13 dispatch entities share one heartbeat keyed by plant_id, so stopping it
    # on any single entity's removal (e.g. disabling "SOC Upper Limit") would kill
    # dispatch for the whole plant. Teardown is handled once by async_unload_entry (#112).

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the pending auto-revert deadline so it survives a restart (#157)."""
        if self.param == "charge_discharge_command" and self._revert_deadline is not None:
            return {"revert_at": self._revert_deadline}
        return None

    def _revert_seconds(self) -> float:
        """Configured auto-revert timeout in seconds (0 = disabled)."""
        try:
            minutes = float(getattr(self.coordinator, "forced_dispatch_duration_minutes", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return minutes * 60

    def _arm_revert(self, *, deadline: float | None = None) -> None:
        """Schedule the revert-to-Stop, replacing any pending one.

        ``deadline`` (epoch seconds) is supplied when restoring across a restart;
        otherwise it's computed from the configured duration. A duration of 0 disables
        auto-revert and leaves nothing scheduled.
        """
        self._cancel_revert()
        if deadline is None:
            seconds = self._revert_seconds()
            if seconds <= 0:
                return
            deadline = time.time() + seconds
        self._revert_deadline = deadline
        delay = max(0.0, deadline - time.time())
        self._revert_cancel = async_call_later(self.hass, delay, self._handle_revert)

    @callback
    def _handle_revert(self, _now: Any) -> None:
        """async_call_later fired — run the async revert."""
        self._revert_cancel = None
        self.hass.async_create_task(self._do_revert())

    async def _do_revert(self) -> None:
        """Revert a forced Charge/Discharge to Stop and stop the heartbeat (#157)."""
        _LOGGER.info("Forced-dispatch timeout for %s: reverting to Stop", self.device_uuid)
        stop_option = "Stop"
        try:
            await self.control.async_update_parameters(self.device_uuid, {self.param: self.options_map[stop_option]})
        except PySolarCloudException as err:
            _LOGGER.warning("Auto-revert to Stop failed for %s: %s", self.device_uuid, err)
        entry = self.coordinator.config_entry
        if entry is not None:
            await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
        self._revert_deadline = None
        self._revert_cancel = None
        self._attr_current_option = stop_option
        self.async_write_ha_state()

    def _cancel_revert(self) -> None:
        """Cancel any pending auto-revert."""
        self._revert_deadline = None
        if self._revert_cancel is not None:
            self._revert_cancel()
            self._revert_cancel = None
