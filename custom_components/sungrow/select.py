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
from homeassistant.helpers import issue_registry as ir
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

# Energy Management Mode (param 10003, Appendix 10). Charge/discharge command & power
# only actuate when the plant is *not* in Self-consumption — writing 10004/10005 alone
# is accepted by the device but ignored until the mode switches (issue #231).
# Compulsory (2) is the portal "Forced mode" path confirmed on residential SH hybrids.
# Named writes require sungrow-isolarcloud >= 0.10.4 (re-enabled 10003 name map).
_EMS_MODE_PARAM = "energy_management_mode"
_EMS_MODE_SELF_CONSUMPTION = Control.encode_parameter("energy_management_mode", "self_consumption")
_EMS_MODE_COMPULSORY = Control.encode_parameter("energy_management_mode", "compulsory")
_CDC_PARAM = "charge_discharge_command"

# Unified battery-mode select (#255). Replaces the raw charge/discharge command select.
BATTERY_MODE_PARAM = "battery_mode"
BATTERY_MODE_SELF_CONSUMPTION = "Self-consumption"
BATTERY_MODE_FORCE_CHARGE = "Force charge"
BATTERY_MODE_FORCE_DISCHARGE = "Force discharge"
BATTERY_MODE_STOP = "Stop"
BATTERY_MODE_FORCED = frozenset({BATTERY_MODE_FORCE_CHARGE, BATTERY_MODE_FORCE_DISCHARGE})
BATTERY_MODE_SAFE = frozenset({BATTERY_MODE_SELF_CONSUMPTION, BATTERY_MODE_STOP})
# Service / automation mode keys (snake_case) → display options.
BATTERY_MODE_SERVICE_KEYS: dict[str, str] = {
    "self_consumption": BATTERY_MODE_SELF_CONSUMPTION,
    "force_charge": BATTERY_MODE_FORCE_CHARGE,
    "force_discharge": BATTERY_MODE_FORCE_DISCHARGE,
    "stop": BATTERY_MODE_STOP,
}
# Restore states from the pre-#255 charge_discharge_command select.
_LEGACY_BATTERY_MODE_STATES: dict[str, str] = {
    "Charge": BATTERY_MODE_FORCE_CHARGE,
    "Discharge": BATTERY_MODE_FORCE_DISCHARGE,
    "Stop": BATTERY_MODE_STOP,
    "charge": BATTERY_MODE_FORCE_CHARGE,
    "discharge": BATTERY_MODE_FORCE_DISCHARGE,
    "stop": BATTERY_MODE_STOP,
}

# Post-write actuation check (#254). After commanding a forced mode we read Energy
# Management Mode (10003) back and, if it is *still* Self-consumption, the inverter did
# not enter Forced mode (the #231 failure) — the write was accepted but had no effect.
# The read is deferred briefly so the device has time to apply the change.
DISPATCH_VERIFY_DELAY = 15
_NOT_ACTUATED_ISSUE = "dispatch_not_actuated"
_REPAIR_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"

# Select parameters exposed as HA Select entities.
DISPATCH_SELECTS: dict[str, dict[str, Any]] = {
    # Single battery mode (#255): Self-consumption / Force charge / Force discharge / Stop.
    # Writes charge_discharge_command (10004) + energy_management_mode (10003) together.
    BATTERY_MODE_PARAM: {
        "options_map": {
            BATTERY_MODE_SELF_CONSUMPTION: "self_consumption",
            BATTERY_MODE_FORCE_CHARGE: "force_charge",
            BATTERY_MODE_FORCE_DISCHARGE: "force_discharge",
            BATTERY_MODE_STOP: "stop",
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
    # Write-only: the inverter's current dispatch mode isn't read back from the API, so
    # the shown option is the last one we commanded — an assumption, not a device reading.
    _attr_assumed_state = True

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
        # Pending post-write actuation check (#254); command select only.
        self._verify_cancel: CALLBACK_TYPE | None = None
        # Set once the entity is removed (e.g. an entry reload) so an already-fired
        # auto-revert task doesn't act on the plant after this instance is gone (#157).
        self._removed = False
        # Throttle counter for the periodic EMS-mode read-back (#286). Only the battery-
        # mode select reads back; other selects leave this at 0.
        self._readback_counter: int = 0

    def _normalize_restored_option(self, state: str) -> str | None:
        """Map a restored state to a current option (including pre-#255 legacy names)."""
        if state in self.options_map:
            return state
        if self.param == BATTERY_MODE_PARAM:
            mapped = _LEGACY_BATTERY_MODE_STATES.get(state)
            if mapped in self.options_map:
                return mapped
        return None

    async def async_added_to_hass(self) -> None:
        """Restore the last selected option across restarts.

        Dispatch commands are write-only (not polled back from the API), so the
        last option the user chose is restored from state rather than fetched.
        """
        await super().async_added_to_hass()
        # Register battery-mode selects so ``sungrow.set_battery_mode`` can find them (#255).
        if self.param == BATTERY_MODE_PARAM and self.entity_id:
            registry = self.hass.data.setdefault(DOMAIN, {}).setdefault("battery_mode_selects", {})
            registry[self.entity_id] = self
        last = await self.async_get_last_state()
        if last is None:
            return
        restored = self._normalize_restored_option(last.state)
        if restored is None:
            return
        self._attr_current_option = restored
        # If we were mid force-charge/discharge before the restart, resume the EMS
        # heartbeat: restoring current_option alone would leave the UI showing a
        # forced mode while the inverter silently times out of External-EMS mode.
        # Only the battery-mode select owns the heartbeat, so this restarts it
        # exactly once per plant; async_start_heartbeat is idempotent (it stops
        # any existing loop first), so a stale loop is never double-started (#112).
        if self.param == BATTERY_MODE_PARAM and restored in BATTERY_MODE_FORCED:
            entry = self.coordinator.config_entry
            assert entry is not None
            # Restore the auto-revert deadline first (#157): if the forced command
            # already timed out while HA was down, revert to Self-consumption instead
            # of resuming dispatch; otherwise resume the heartbeat and re-arm.
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
        """Cancel the auto-revert and actuation-check timers when the select is removed."""
        self._removed = True
        self._cancel_revert()
        self._cancel_verify()
        if self.param == BATTERY_MODE_PARAM and self.entity_id:
            registry = self.hass.data.get(DOMAIN, {}).get("battery_mode_selects")
            if isinstance(registry, dict):
                registry.pop(self.entity_id, None)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle a coordinator update — includes throttled EMS-mode read-back (#286).

        Every 3rd coordinator update (~15 min at default 5-min poll), the battery-mode
        select reads the inverter's actual EMS mode back. If the portal or an external
        automation changed the mode, the entity reflects the device truth instead of the
        stale assumed state.
        """
        super()._handle_coordinator_update()
        if self.param != BATTERY_MODE_PARAM:
            return
        self._readback_counter += 1
        if self._readback_counter < 3:
            return
        self._readback_counter = 0
        self.hass.async_create_task(self._async_dispatch_readback())

    async def _async_dispatch_readback(self) -> None:
        """Read the inverter's EMS mode and reconcile with the assumed state (#286)."""
        if self._removed:
            return
        still_self = await self._read_still_self_consumption()
        if still_self is None:
            return  # Unknown — don't change anything.
        current = self._attr_current_option
        if still_self and current in BATTERY_MODE_FORCED:
            # The inverter is in Self-consumption but we think it's forced — the portal
            # or a timeout reverted it externally. Sync the entity state.
            _LOGGER.info(
                "EMS read-back for %s shows Self-consumption; updating from assumed %s",
                self.device_uuid,
                current,
            )
            self._attr_current_option = BATTERY_MODE_SELF_CONSUMPTION
            self._cancel_revert()
            entry = self.coordinator.config_entry
            if entry is not None:
                await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
            self._clear_not_actuated_issue()
            self.async_write_ha_state()
        elif not still_self and current in BATTERY_MODE_SAFE:
            # The inverter is in a forced/compulsory mode but we think it's safe — an
            # external tool forced it. We can't know which forced option, so just note
            # it in the log. The entity stays at its current option (Self-consumption/Stop)
            # since we don't know if it's charge or discharge.
            _LOGGER.debug(
                "EMS read-back for %s shows Forced mode but entity shows %s; "
                "an external tool may have changed the mode",
                self.device_uuid,
                current,
            )

    @staticmethod
    def _restore_revert_deadline(raw: Any) -> float | None:
        """Parse a restored ``revert_at`` epoch attribute, or None if absent/invalid."""
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _command_payload(self, option: str, value: str) -> dict[str, str]:
        """Build the param write for a select option.

        Battery mode (#255) writes charge_discharge_command (10004) together with
        Energy Management Mode (10003). Without leaving Self-consumption, the inverter
        accepts 10004/10005 writes but continues normal self-consumption operation
        (#231). Compulsory mode matches the portal "Forced mode" tile; safe modes
        restore Self-consumption so the plant returns to default behaviour.
        """
        if self.param == BATTERY_MODE_PARAM:
            if option == BATTERY_MODE_FORCE_CHARGE:
                return {
                    _CDC_PARAM: Control.CHARGE_DISCHARGE_COMMANDS["charge"],
                    _EMS_MODE_PARAM: _EMS_MODE_COMPULSORY,
                }
            if option == BATTERY_MODE_FORCE_DISCHARGE:
                return {
                    _CDC_PARAM: Control.CHARGE_DISCHARGE_COMMANDS["discharge"],
                    _EMS_MODE_PARAM: _EMS_MODE_COMPULSORY,
                }
            # Self-consumption and Stop both return the plant to the safe default.
            return {
                _CDC_PARAM: Control.CHARGE_DISCHARGE_COMMANDS["stop"],
                _EMS_MODE_PARAM: _EMS_MODE_SELF_CONSUMPTION,
            }
        return {self.param: value}

    async def async_select_option(self, option: str, *, duration_minutes: float | None = None) -> None:
        """Update the dispatch parameter on the inverter.

        ``duration_minutes`` (battery mode only) overrides the auto-revert timeout for
        this forced command without changing the Forced Dispatch Duration number — used
        by the ``sungrow.set_battery_mode`` service for tariff automations (#255).
        """
        value = self.options_map[option]
        payload = self._command_payload(option, value)
        _LOGGER.debug("Setting %s to %s (%s) for %s", self.param, option, payload, self.device_uuid)
        try:
            await self.control.async_update_parameters(self.device_uuid, payload)
        except PySolarCloudException as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="dispatch_write_failed",
                translation_placeholders={"param": self.param, "error": str(err)},
            ) from err
        # Remember the selected option so the UI reflects it and it survives restarts.
        self._attr_current_option = option

        # Keep the inverter in dispatch mode while force-charging/discharging.
        entry = self.coordinator.config_entry
        assert entry is not None
        if self.param == BATTERY_MODE_PARAM and option in BATTERY_MODE_FORCED:
            await async_start_heartbeat(
                self.hass,
                entry,
                self.coordinator.plant_id,
                self.device_uuid,
                interval=60,
            )
            # Arm the auto-revert so this forced command can't silently persist (#157/#255).
            if duration_minutes is not None and duration_minutes > 0:
                self._arm_revert(deadline=time.time() + float(duration_minutes) * 60)
            elif duration_minutes is not None and duration_minutes <= 0:
                # Explicit 0 from the service: no auto-revert for this command.
                self._cancel_revert()
            else:
                self._arm_revert()
            # Verify the inverter actually entered Forced mode shortly after (#254): the
            # write can be accepted while the plant stays in Self-consumption (#231).
            self._schedule_actuation_check()
        elif self.param == BATTERY_MODE_PARAM and option in BATTERY_MODE_SAFE:
            await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
            self._cancel_revert()
            # Stopping dispatch clears any "not actuated" Repair and pending check (#254).
            self._cancel_verify()
            self._clear_not_actuated_issue()

    # NOTE: the heartbeat is deliberately NOT stopped from async_will_remove_from_hass.
    # All ~13 dispatch entities share one heartbeat keyed by plant_id, so stopping it
    # on any single entity's removal (e.g. disabling "SOC Upper Limit") would kill
    # dispatch for the whole plant. Teardown is handled once by async_unload_entry (#112).

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the pending auto-revert deadline so it survives a restart (#157)."""
        if self.param == BATTERY_MODE_PARAM and self._revert_deadline is not None:
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
        """Revert a forced mode to Self-consumption and stop the heartbeat (#157/#255)."""
        if self._removed:
            # The timer fired, but the entity was removed (e.g. an entry reload) before
            # this task ran. Acting now would stop a freshly-restored heartbeat and write
            # state on a dead entity, so bail out — the new instance owns the heartbeat.
            return
        _LOGGER.info(
            "Forced-dispatch timeout for %s: reverting to %s",
            self.device_uuid,
            BATTERY_MODE_SELF_CONSUMPTION,
        )
        safe_option = BATTERY_MODE_SELF_CONSUMPTION
        try:
            await self.control.async_update_parameters(
                self.device_uuid,
                self._command_payload(safe_option, self.options_map[safe_option]),
            )
        except PySolarCloudException as err:
            _LOGGER.warning("Auto-revert to %s failed for %s: %s", safe_option, self.device_uuid, err)
        entry = self.coordinator.config_entry
        if entry is not None:
            await async_stop_heartbeat(self.hass, entry, self.coordinator.plant_id)
        self._revert_deadline = None
        self._revert_cancel = None
        # Reverting to a safe mode makes any "not actuated" Repair moot (#254).
        self._cancel_verify()
        self._clear_not_actuated_issue()
        self._attr_current_option = safe_option
        self.async_write_ha_state()

    def _cancel_revert(self) -> None:
        """Cancel any pending auto-revert."""
        self._revert_deadline = None
        if self._revert_cancel is not None:
            self._revert_cancel()
            self._revert_cancel = None

    # --- Post-write actuation verification (#254) ---------------------------------

    def _schedule_actuation_check(self) -> None:
        """Schedule a deferred check that the inverter actually entered Forced mode."""
        self._cancel_verify()
        self._verify_cancel = async_call_later(self.hass, DISPATCH_VERIFY_DELAY, self._handle_actuation_check)

    @callback
    def _handle_actuation_check(self, _now: Any) -> None:
        """async_call_later fired — run the async actuation check."""
        self._verify_cancel = None
        self.hass.async_create_task(self._verify_actuation())

    def _cancel_verify(self) -> None:
        """Cancel any pending actuation check."""
        if self._verify_cancel is not None:
            self._verify_cancel()
            self._verify_cancel = None

    @staticmethod
    def _reads_as_self_consumption(params: list[dict[str, Any]]) -> bool | None:
        """Interpret an EMS-mode read-back: True = still Self-consumption, None = unknown.

        Conservative on purpose (#254): only a *positive* Self-consumption read-back means
        "not actuated". A numeric ``0`` or a name containing "self" is Self-consumption; any
        other recognised mode (compulsory/forced/external/vpp) is actuated → ``False``; an
        empty/absent/unparseable value is ``None`` (unknown) so we never raise a false alarm.
        """
        for param in params:
            if str(param.get("id")) == "10003" or param.get("code") == _EMS_MODE_PARAM:
                raw = param.get("value")
                if raw is None:
                    return None
                try:
                    return float(raw) == 0
                except (TypeError, ValueError):
                    pass
                text = str(raw).strip().lower()
                if not text or text in {"none", "null"}:
                    return None
                # Self-consumption reads as a name containing "self"; any other named
                # mode (compulsory/forced/external/vpp) means the inverter actuated.
                return "self" in text
        return None

    async def _read_still_self_consumption(self) -> bool | None:
        """Read Energy Management Mode back. True/False = still/not Self-consumption; None = unknown.

        Best-effort and fail-safe: any read error or ambiguous value returns ``None`` so a
        flaky/limited read-back never drives a false verdict on a battery-control path.
        """
        try:
            params = await self.control.async_read_parameters(self.device_uuid, [_EMS_MODE_PARAM])
        except Exception as err:  # pylint: disable=broad-except  (best-effort verification)
            _LOGGER.debug("EMS-mode read-back failed for %s; skipping actuation check: %s", self.device_uuid, err)
            return None
        return self._reads_as_self_consumption(params)

    async def _verify_actuation(self) -> None:
        """Confirm the inverter left Self-consumption; retry once, then flag if it didn't.

        Follows the cloud-actuation Confirm → Retry → Notify discipline: a write can be
        accepted while the plant stays in Self-consumption (#231). If the read-back confirms
        it never switched, re-issue the forced-mode write **once** before raising a Repair —
        and default to no action on an unknown/errored read-back (never a false alarm on a
        battery-control path). Only a positive Self-consumption read-back raises the Repair.
        """
        if self._removed or self._attr_current_option not in BATTERY_MODE_FORCED:
            return
        still_self = await self._read_still_self_consumption()
        if still_self is not True:
            # Actuated (False) → clear any prior Repair; unknown (None) → leave it as-is so a
            # real, still-valid warning is not dismissed by a flaky read.
            if still_self is False:
                self._clear_not_actuated_issue()
            return

        # Confirmed still in Self-consumption: retry the forced-mode write once.
        option = self._attr_current_option
        assert option is not None
        _LOGGER.debug("Dispatch %s for %s not actuated; re-issuing forced mode", option, self.device_uuid)
        try:
            await self.control.async_update_parameters(
                self.device_uuid, self._command_payload(option, self.options_map[option])
            )
        except PySolarCloudException as err:
            _LOGGER.debug("Forced-mode retry write failed for %s: %s", self.device_uuid, err)

        still_self = await self._read_still_self_consumption()
        if still_self is True:
            _LOGGER.warning(
                "Dispatch command %s for %s was accepted but the inverter is still in "
                "Self-consumption after a retry — it did not enter Forced mode",
                option,
                self.device_uuid,
            )
            self._raise_not_actuated_issue()
        elif still_self is False:
            self._clear_not_actuated_issue()

    def _raise_not_actuated_issue(self) -> None:
        """Raise the 'dispatch not actuated' Repair for this plant (#254)."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{_NOT_ACTUATED_ISSUE}_{self.coordinator.plant_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=_NOT_ACTUATED_ISSUE,
            translation_placeholders={"plant": self.coordinator.plant_name},
            learn_more_url=_REPAIR_LEARN_MORE,
        )

    def _clear_not_actuated_issue(self) -> None:
        """Clear the 'dispatch not actuated' Repair for this plant (#254)."""
        ir.async_delete_issue(self.hass, DOMAIN, f"{_NOT_ACTUATED_ISSUE}_{self.coordinator.plant_id}")
