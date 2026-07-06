# Spec — #158: model each physical device as a nested HA device

- **Issue:** [#158](https://github.com/KRoperUK/sungrow-hass/issues/158) (4.0.0 milestone)
- **Date:** 2026-07-06
- **Approved decisions:** Full re-home · Non-breaking (keep unique_ids) · Singular-type-only aggregation rule

### Live validation (2026-07-06, plant 1000002)

A raw `getDeviceListByPsId` + `getPowerStationRealTimeData` probe against a real plant
confirmed:

- **No firmware/version field exists** in the device-list payload (fields: `chnnl_id`,
  `claim_state`, `communication_dev_sn`, `dev_fault_status`, `dev_status`, `device_code`,
  `device_model_code`, `device_model_id`, `device_name`, `device_sn`, `device_type`,
  `factory_name`, `grid_connection_date`, `ps_id`, `ps_key`, `type_name`, `uuid`). So
  **firmware enrichment is dropped from this spec** (§5) — model/serial/manufacturer stay.
- The plant is the canonical case: 1 INVERTER (SG3.6RS), 1 METER (SGSmartMeter), 1
  COMMUNICATION_MODULE (WiNet-S), **no battery device** — so the ~25 battery/ESS codes in
  the realtime payload correctly stay on the plant (0 matches), exactly as the singular
  rule intends.
- The point→device map (§1) is now grounded in the **74 real point codes** the plant
  returned, not guesswork.

## Background

Nesting was largely delivered in #149/#162, so this is a smaller change than the
issue implies:

- `build_device_info()` already builds per-device `DeviceInfo` with
  `identifiers={(DOMAIN, uuid)}`, `via_device=(DOMAIN, plant_id)`, model, serial and
  manufacturer.
- `binary_sensor.py` already registers a nested HA device for **every** discovered
  device with a UUID (Fault + Connectivity), so the inverter/battery/meter/WiNet-S
  cards already exist under the plant.
- Per-device diagnostic sensors and dispatch number/select entities already attach
  to those nested devices.

The one thing still flat is the **plant-level realtime sensors** — the ~30 sensors
most users actually see (`total_active_power`, `battery_soc`, `load_power`, grid and
meter values …). They all sit on the single plant "service" device because the plant
realtime payload is device-agnostic (`{code: {value, unit, name, id}}`, no device
attribution), and the measure-point catalog carries no device type either.

## Goal

Re-home each flat plant sensor onto the physical device it belongs to, so the HA
device tree reflects the hardware — **without** changing any entity's identity, and
add firmware to device cards where the cloud exposes it.

```
Plant "Home"  (service device — anchors the tree)
├─ Inverter SG10RT     ← AC/DC power, daily yield, MPPT, grid freq, temp, Fault/Conn
├─ Battery SBR128      ← SOC, battery power, charge/discharge energy, Fault/Conn
├─ Meter               ← grid power, import/export energy, Fault/Conn
└─ WiNet-S             ← WLAN/wireless signal, Fault/Conn
```

## Design

### 1. Curated point-code → device-type map (`const.py`)

A new `POINT_DEVICE_TYPE` maps a canonical plant point code to the set of device
types that may own it. A `frozenset` of types (not a single type) lets a hybrid ESS
device satisfy both PV and battery points.

```python
_PV_TYPES      = frozenset({DeviceType.INVERTER, DeviceType.MICROINVERTER,
                            DeviceType.ENERGY_STORAGE_SYSTEM, DeviceType.ENERGY_STORAGE_SYSTEM_2})
_BATTERY_TYPES = frozenset({DeviceType.ENERGY_STORAGE_SYSTEM, DeviceType.ENERGY_STORAGE_SYSTEM_2,
                            DeviceType.BATTERY})
_METER_TYPES   = frozenset({DeviceType.METER, DeviceType.GRID_CONNECTION_POINT})

# Keys are literal plant point codes (CODE_ALIASES is code->display-name, NOT a
# canonicaliser, so we list each known code directly). Grounded in the 74 real
# codes from plant 1000002 plus the documented codes in docs/SENSORS.md.
POINT_DEVICE_TYPE: dict[str, frozenset[DeviceType]] = {
    # PV / inverter
    "total_active_power": _PV_TYPES, "total_active_power_of_pv": _PV_TYPES,
    "inverter_ac_power": _PV_TYPES, "inverter_ac_power_normalization": _PV_TYPES,
    "inverter_daily_yield": _PV_TYPES, "inverter_total_yield": _PV_TYPES,
    "daily_yield": _PV_TYPES, "total_yield": _PV_TYPES, "total_pv_yield": _PV_TYPES,
    "daily_pv_yield_ems": _PV_TYPES, "pv_active_power_ems": _PV_TYPES,
    "total_dc_power": _PV_TYPES,
    # Battery / ESS
    "battery_level_soc": _BATTERY_TYPES, "battery_soc": _BATTERY_TYPES,
    "total_field_soc": _BATTERY_TYPES, "energy_storage_soc_ems": _BATTERY_TYPES,
    "total_field_energy_storage_active_power": _BATTERY_TYPES,
    "total_field_maximum_rechargeable_power": _BATTERY_TYPES,
    "total_field_maximum_dischargeable_power": _BATTERY_TYPES,
    "total_field_chargeable_energy": _BATTERY_TYPES,
    "total_field_dischargeable_energy": _BATTERY_TYPES,
    "daily_field_charge_capacity": _BATTERY_TYPES,
    "daily_field_discharge_capacity": _BATTERY_TYPES,
    "energy_storage_active_power_ems": _BATTERY_TYPES,
    "battery_charge_power": _BATTERY_TYPES, "battery_discharge_power": _BATTERY_TYPES,
    "battery_level": _BATTERY_TYPES, "battery_soh": _BATTERY_TYPES, ...
    # Meter / grid
    "grid_active_power": _METER_TYPES, "grid_active_power_ems": _METER_TYPES,
    "meter_ac_power": _METER_TYPES, "meter_active_power": _METER_TYPES,
    "feed_in_energy_today": _METER_TYPES, "feed_in_energy_total": _METER_TYPES,
    "daily_feed_in_energy_pv": _METER_TYPES, "energy_purchased_today": _METER_TYPES,
    "total_purchased_energy": _METER_TYPES,
    "accumulative_power_consumption_by_meter": _METER_TYPES,
    "meter_forward_active_energy": _METER_TYPES,
    "meter_reverse_active_energy": _METER_TYPES, ...
}
```

Rules:

- Keys are **literal point codes** (variant spellings each get their own key — there is
  no canonicaliser). The full list is enumerated in the plan; the `...` above is
  illustrative.
- **Load points** (`load_power`, `total_load_active_power`, `*_load_consumption`),
  **derived ratios** (`plant_pr`, `meter_pr`, `inverter_pr`), **forecast/environment**
  (`power_forecast`, `daily_irradiation`, `plant_*`) and generic **plant aggregates**
  (`power`, `power_fraction`) are deliberately *absent* — a household load is not a
  device and ratios/forecasts are plant analytics, so those stay on the plant.
- **Any code not in the map stays on the plant device.** Safe default; adding a code
  later is a one-line map entry.

### 2. Device resolution — singular-type rule (`__init__.py`)

```python
def resolve_point_device(point_code, devices) -> dict | None:
    """Return the single physical device that owns a plant point, else None (=plant)."""
    types = POINT_DEVICE_TYPE.get(canonical_code(point_code))
    if types is None:
        return None
    matches = [d for d in devices
               if d.get("uuid") and any(_matches_device_type(d, t) for t in types)]
    return matches[0] if len(matches) == 1 else None
```

Exactly one matching device → re-home there. Zero or more than one → `None` → the
point stays on the plant device. This keeps aggregate points correct on multi-inverter
plants (2 inverters ⇒ "Total Active Power" is a genuine sum, so it stays on the plant)
while giving the typical 1-inverter / 1-battery / 1-meter home a clean tree.

### 3. Non-breaking identity

`SungrowSensor` keeps `unique_id = f"{plant_id}_{point_code}"` unchanged. Only its
`device_info` is chosen by `resolve_point_device`:

```python
device = resolve_point_device(point_code, coordinator.devices)
self._attr_device_info = (
    build_device_info(device, plant_id, fallback_name=plant_name)
    if device is not None else _plant_device_info(plant_id, plant_name, console_url)
)
```

When an already-registered entity is re-added with different `device_info.identifiers`,
HA re-associates it to the new device automatically: **entity_id, long-term history and
automations are all preserved** — only the device-card grouping changes. No
`async_migrate_entry` entity work is required. (A registry test will assert this.)

### 4. Plant device stays the anchor

`via_device=(DOMAIN, plant_id)` needs the plant device to exist even if every sensor
re-homes off it. Today the plant device is created implicitly by `SungrowSensor`; once
sensors move away that is no longer guaranteed. So register the plant device
**explicitly** at setup, per coordinator:

```python
dr.async_get_or_create(
    config_entry_id=entry.entry_id,
    identifiers={(DOMAIN, plant_id)},
    name=plant_name, manufacturer="Sungrow",
    entry_type=dr.DeviceEntryType.SERVICE,
    configuration_url=console_url,
)
```

The `console_url` derivation (region → `GATEWAY_CONSOLE_URLS`) moves from `sensor.py`
into `__init__.py` setup; `sensor.py`'s plant fallback reuses it.

### 5. Firmware enrichment — DROPPED

The live probe confirmed `getDeviceListByPsId` exposes **no firmware/version field**, so
firmware is out of scope for #158. `build_device_info` is unchanged (model, serial,
manufacturer only). Surfacing firmware would require an extra per-device detail call,
which conflicts with the rate-limit ethos — deferred to a later issue if ever wanted.

## Files changed

| File | Change |
|---|---|
| `const.py` | New `POINT_DEVICE_TYPE` map + `_PV_TYPES`/`_BATTERY_TYPES`/`_METER_TYPES` constants. |
| `__init__.py` | `resolve_point_device()`; explicit plant-device registration at setup; `_plant_device_info` helper (shared with sensor). |
| `sensor.py` | `SungrowSensor.__init__` selects plant vs device `device_info` via the resolver; unique_id untouched. |
| `tests/` | Map/resolver unit tests; singular-vs-multi-device re-home; **non-breaking** unique_id + entity_id preservation on device move; plant-device anchor exists when all sensors re-home. |

## Testing

- `resolve_point_device`: mapped+singular → device; mapped+0 → plant; mapped+2 → plant;
  unmapped → plant; hybrid ESS satisfies both PV and battery points.
- Re-home preserves `unique_id` and `entity_id` (registry assertion) — the crux of the
  non-breaking guarantee.
- Plant device is registered even when every plant sensor re-homes.
- Per-device sensors (opt-in) still skip plant-level codes, so no duplicate sensor is
  created on a device that also received a re-homed plant sensor.

## Out of scope

- Per-device realtime as the default data source (rate-limit heavy — deferred to #159's
  local-Modbus transport). **No new API calls are added by this change.**
- Any unique_id / entity_id renaming (explicitly rejected — non-breaking chosen).

## Risks / open questions

1. **Point-map coverage** — grounded in 74 real codes + docs; unmapped codes fall back
   to the plant device, so under-coverage is harmless (just less re-homing), never wrong.
2. **Existing installs** — on upgrade, HA moves entities to their new device on first
   reload; the now-emptier plant device persists intentionally (explicitly registered).
   No user action, no history loss.
