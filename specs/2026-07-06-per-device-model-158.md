# Spec — #158: model each physical device as a nested HA device

- **Issue:** [#158](https://github.com/KRoperUK/sungrow-hass/issues/158) (4.0.0 milestone)
- **Date:** 2026-07-06
- **Approved decisions:** Full re-home · Non-breaking (keep unique_ids) · Singular-type-only aggregation rule

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

POINT_DEVICE_TYPE: dict[str, frozenset[DeviceType]] = {
    # PV / inverter
    "total_active_power": _PV_TYPES, "inverter_ac_power": _PV_TYPES,
    "total_dc_power": _PV_TYPES, "daily_yield": _PV_TYPES,
    "inverter_daily_yield": _PV_TYPES, "daily_pv_yield_ems": _PV_TYPES, ...
    # Battery
    "total_field_energy_storage_active_power": _BATTERY_TYPES,
    "battery_level_soc": _BATTERY_TYPES, "battery_soc": _BATTERY_TYPES,
    "total_field_soc": _BATTERY_TYPES, "energy_storage_soc_ems": _BATTERY_TYPES,
    "total_field_maximum_rechargeable_power": _BATTERY_TYPES, ...
    # Meter / grid
    "grid_active_power": _METER_TYPES, "grid_active_power_ems": _METER_TYPES,
    "feed_in_energy_today": _METER_TYPES, "energy_purchased_today": _METER_TYPES, ...
}
```

Rules:

- The initial code list is seeded from `docs/SENSORS.md` "Common dashboard values" +
  "Battery-specific points", resolved through the existing `CODE_ALIASES` so variant
  spellings collapse to one key.
- **Load points** (`load_power`, `total_load_active_power`) and **plant aggregates**
  (plant power, installed capacity, performance ratio) are deliberately *absent* — a
  household load is not a device, so those stay on the plant.
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

### 5. Firmware enrichment (best-effort)

`build_device_info` gains `sw_version=device.get(<field>)`. The exact field from
`getDeviceListByPsId` is **unconfirmed** — first implementation task is to check the
iSolarCloud docs (MCP `isolarcloud` server) / a live payload. If no firmware field
exists, `sw_version` is simply omitted — never faked. This sub-item can ship
independently if the field is absent.

## Files changed

| File | Change |
|---|---|
| `const.py` | New `POINT_DEVICE_TYPE` map + type-set constants. |
| `__init__.py` | `resolve_point_device()`; `build_device_info(... sw_version)`; explicit plant-device registration at setup; `_plant_device_info` helper (shared with sensor). |
| `sensor.py` | `SungrowSensor.__init__` selects plant vs device `device_info` via the resolver; unique_id untouched. |
| `tests/` | Map/resolver unit tests; singular-vs-multi-device re-home; **non-breaking** unique_id + entity_id preservation on device move; firmware passthrough; plant-device anchor exists when all sensors re-home. |

## Testing

- `resolve_point_device`: mapped+singular → device; mapped+0 → plant; mapped+2 → plant;
  unmapped → plant; hybrid ESS satisfies both PV and battery points.
- Re-home preserves `unique_id` and `entity_id` (registry assertion) — the crux of the
  non-breaking guarantee.
- Firmware present → `sw_version` set; absent → omitted.
- Plant device is registered even when every plant sensor re-homes.
- Per-device sensors (opt-in) still skip plant-level codes, so no duplicate sensor is
  created on a device that also received a re-homed plant sensor.

## Out of scope

- Per-device realtime as the default data source (rate-limit heavy — deferred to #159's
  local-Modbus transport). **No new API calls are added by this change.**
- Any unique_id / entity_id renaming (explicitly rejected — non-breaking chosen).

## Risks / open questions

1. **Firmware field availability** — resolved during implementation; degrades to
   "omit" if absent.
2. **Point-map coverage** — starts with the documented common codes; unmapped codes
   fall back to the plant device, so under-coverage is harmless (just less re-homing),
   never wrong.
3. **Existing installs** — on upgrade, HA moves entities to their new device on first
   reload; the now-emptier plant device persists intentionally (explicitly registered).
   No user action, no history loss.
