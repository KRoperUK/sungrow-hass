# Comprehensive measuring-point mapping — design

**Issue:** [#105](https://github.com/KRoperUK/sungrow-hass/issues/105) — Surface documented battery/charger/meter measuring points as sensor aliases.
**Status:** design approved, pending implementation.
**Date:** 2026-07-03.

## Problem

The integration turns every realtime measure point iSolarCloud returns into a Home
Assistant sensor. Two gaps remain after PR #106 (which aliased a first batch of
battery/charger/meter codes):

1. **Opaque / inconsistent names.** Many built-in default codes render as
   title-cased codes (`Meter Pr`, `Total Field Power Factor`, `Ess Daily Charge Ems`),
   and points that arrive as a raw numeric point ID (uncatalogued per-device points)
   fall back to the API `name`, which is frequently Chinese. Only a handful of codes
   have friendly aliases today.

2. **Dimensionless numerics degrade to text.** In the official catalogs, SOC, SOH,
   power factor, PR (performance ratio), plant-power fraction, self-consumption rate
   and charge/discharge counts all have **blank units**. When the realtime API sends
   no unit, `infer_device_class()` returns `(None, None)`, and `native_value` then
   returns the value as a **string** — so these numeric values silently become text
   sensors that never graph and never reach the Energy dashboard. Additionally,
   `battery_soh` matches the `"battery"` percentage hint and is mislabelled
   `SensorDeviceClass.BATTERY` (that class means charge level, not health).

The [mcp-isolarcloud](https://github.com/KRoperUK/mcp-isolarcloud) docs server now
exposes the authoritative per-device measuring-point catalogs (point ID, name, unit),
so we can ground a comprehensive, correctly-classified mapping.

## Scope (confirmed with maintainer)

- **Coverage:** comprehensive — every documented point across all 17 device catalogs
  (battery, charger, energy-meter, inverter, energy-storage-inverter, plant,
  combiner-box, EMS, PCS, CMU, BSC, LC, microinverter, environment, comms-device,
  comms-module, iHomeManage).
- **Classification:** add a code/point-ID classification layer so dimensionless
  numerics classify correctly; fix the `battery_soh` mis-classification.
- **Status enums:** translate the four points that ship a documented value table
  (charger 33716, ESS-inverter 13146, inverter 29, microinverter 51301) to a
  `SensorDeviceClass.ENUM` with human-readable states.

## Architecture

### New module: `custom_components/sungrow/measure_points.py`

The comprehensive catalog is large, mechanically-derived data; isolating it keeps
`sensor.py` focused on entity behaviour. The module owns three data structures plus
pure resolver functions (no Home Assistant runtime dependencies beyond the
`SensorDeviceClass`/`SensorStateClass` enums, so it is trivially unit-testable).

**`POINT_CATALOG: dict[str, PointInfo]`** — keyed by numeric point-ID **string**, one
entry per documented point. `PointInfo` is a small frozen dataclass / `NamedTuple`:

```python
class PointInfo(NamedTuple):
    name: str                              # official English name
    device_class: SensorDeviceClass | None # pre-derived from doc unit + name
    state_class: SensorStateClass | None   # pre-derived
    options: tuple[str, ...] | None        # set only for ENUM points
```

`device_class`/`state_class` are pre-computed **at authoring time** from the
documented unit and name (see Classification rules) so the runtime path stays simple.
The catalog is committed static data. Point IDs are globally unique per meaning across
catalogs, with the one intentional exception that CMU and BSC share IDs 59008/59010/
59012/59014 for the same cell voltage/temperature points (no conflict).

**`CODE_ALIASES: dict[str, str]`** — replaces today's `SENSOR_ALIASES` +
`EXTRA_CODE_ALIASES`, merged and expanded. Keys are the codes that pysolarcloud
returns for built-in default points, plus the recommended user-supplied codes from
`docs/SENSORS.md`. Values are clean display names, including the previously-ugly
built-ins (`meter_pr` → "Meter PR", `total_field_power_factor` → "Battery Power
Factor", the EMS points, `total_number_of_charge_discharge` → "Battery Charge/Discharge
Cycles", …).

**`ENUM_MAPS: dict[str, dict[int, str]]`** — keyed by point-ID string, value → English
label, for the four documented status points. The catalog's `options` for those points
is derived from the map's values.

### Resolver functions (in `measure_points.py`, consumed by `sensor.py`)

```python
def resolve_name(point_id: str, code: str, api_name: str | None) -> str
def resolve_classification(unit: str | None, code: str, point_id: str)
        -> tuple[SensorDeviceClass | None, SensorStateClass | None]
def resolve_enum_options(point_id: str) -> tuple[str, ...] | None
def resolve_enum_value(point_id: str, value) -> str | None
```

### Naming precedence (in `resolve_name`, minimises churn to existing entities)

1. `CODE_ALIASES[code]` — curated names win.
2. If `code` is numeric/opaque → `POINT_CATALOG[point_id].name` (English) — fixes raw
   numeric IDs and Chinese API names.
3. If `code` is a readable string → `code.replace("_", " ").title()` (today's behaviour,
   so existing string-coded default entities keep their names unless explicitly aliased).
4. Fallback → API `name`, else `Sensor {point_id}`.

### Classification rules

`resolve_classification` — the API's real unit stays authoritative; the catalog and
code keywords are fallbacks for unitless points:

1. **Enum** — `point_id in ENUM_MAPS` → `(ENUM, None)`.
2. **Unit map** — extended `_UNIT_CLASS_MAP` when the API sends a known unit:
   - existing: W/kW/MW/GW, Wh/kWh/MWh/GWh, V/mV/kV, A/mA, Hz, °C/℃/°F, var/kvar, VA/kVA.
   - **added:** `%rh`→HUMIDITY, `m/s`→WIND_SPEED, `hpa`→PRESSURE, `mm`→PRECIPITATION,
     `w/m²`/`w/㎡`→IRRADIANCE, `h`/`H`→DURATION (measurement), `varh`→(None,
     TOTAL_INCREASING), `kω`/`ω`→(None, MEASUREMENT), `°`/`wh/m²`/`w/wp`→(None,
     MEASUREMENT).
3. **Percent** (`%`, `percent`) — SOH/health carve-out → `(None, MEASUREMENT)`;
   soc/battery-level code → `(BATTERY, MEASUREMENT)`; else `(None, MEASUREMENT)`.
4. **Catalog fallback** — unit unknown/blank and `point_id in POINT_CATALOG` →
   the catalog's pre-derived `(device_class, state_class)`.
5. **Code keyword fallback** (for user-supplied codes not in the catalog):
   `power_factor`→`(POWER_FACTOR, MEASUREMENT)`; `soc`/`battery_level`→
   `(BATTERY, MEASUREMENT)`; `soh`/`health`→`(None, MEASUREMENT)`.
6. Else `(None, None)` (text).

**Catalog authoring classifier** (used once, when building `POINT_CATALOG` from the
doc tables — not at runtime): unit present → unit map; blank unit → name keywords:
"SOC"/"Level" → BATTERY/measurement; "SOH"/"Health" → None/measurement; "Power
Factor"/"PF" → POWER_FACTOR/measurement; "PR"/"Rate"/"Fraction"/"Number"/"Normalization"
→ None/measurement (numeric); "Status"/"Mode"/"Version"/"S/N"/"Serial"/"Card ID"/
"Position"/"ID"/"Signal Strength" → None/None (text, safe); else None/None.

### `sensor.py` changes

- `_apply_point_metadata` calls the resolvers, passing `init_data["id"]` (the numeric
  point ID) through, and sets `_attr_options` when the point is an enum (device class
  ENUM, no unit, no state class).
- `native_value` gains an enum branch: `resolve_enum_value(point_id, raw)` maps the int
  to its label; unmapped values fall back to `str(raw)` so a new firmware code never
  crashes.
- Everything else (unique_id, per-plant/per-device grouping, `via_device`,
  disabled-when-Unknown default) is unchanged.
- `infer_device_class` is kept as the public name but its signature gains `point_id`
  (`infer_device_class(unit, code, point_id)`) and it delegates to
  `resolve_classification`. Existing `test_infer_device_class` cases are updated to pass
  a point ID (using `""`/an unknown ID where the case is unit-driven).

### `docs/SENSORS.md`

Regenerated with comprehensive, per-device-type `point_id=code` copy-paste blocks for
the **Extra measure points** option, each linked to its mcp-isolarcloud catalog, plus a
note that device/state class is inferred automatically (energy points feed the Energy
dashboard; SOC/SOH/PF/counts now classify without a unit).

## Testing

- `resolve_classification`: power factor (no unit) → POWER_FACTOR; SOC no-unit →
  BATTERY; SOH → (None, MEASUREMENT), **not** BATTERY; PR/count no-unit → (None,
  MEASUREMENT); enum point → (ENUM, None); new units (%RH, m/s, hPa, mm, W/m², h,
  varh); unknown → (None, None); API unit still wins over catalog.
- `resolve_name`: numeric point ID → catalog English name; alias-by-code precedence;
  readable code keeps title-case; unknown numeric → `Sensor {id}`.
- `native_value`: unitless-numeric now returns `float` (regression vs. old text
  behaviour); enum returns the mapped label; unmapped enum int returns `str`.
- Catalog integrity: every `ENUM_MAPS` key exists in `POINT_CATALOG` with `options`
  set; no `PointInfo` has a state class without also being numeric-capable.
- Keep `fail_under` coverage green; run ruff + mypy + pytest before pushing (CLAUDE.md).

## Migration / compatibility notes

- Some existing default entities change device/state class (e.g.
  `total_field_power_factor` text → POWER_FACTOR numeric; unitless SOC → BATTERY) and a
  few display names become cleaner. **Entity IDs are derived from `unique_id`
  (`{plant_id}_{code}`) and do not change** — only the class/name shift. Called out in
  the PR body.
- No new config-entry options, so `strings.json`/`translations/en.json` need no changes
  unless the implementer adds an entity `translation_key` for enums; the chosen approach
  (English `options` + runtime mapping) needs no translation files.
- `pysolarcloud` stays pinned in `manifest.json`; the token-persistence invariant is
  untouched.

## Out of scope

- Undocumented status/mode points (PCS Work Mode, LC Working Mode, EMS Status, battery
  Operation Status, …) stay as plain text — no enum table exists to translate them.
- Per-point unit conversion or value scaling. The integration surfaces values as the API
  reports them.
