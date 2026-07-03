# Sungrow iSolarCloud sensor mapping

The integration creates one Home Assistant device per iSolarCloud plant, then adds a sensor for every realtime measure point that the plant returns. The point codes come from the iSolarCloud API; this guide maps the most common ones to the values shown in the iSolarCloud app.

> Not every inverter / battery / meter returns every point. The available set depends on your model, firmware, and region. If a value you expect is missing, see [Adding extra measure points](#adding-extra-measure-points) below.

## Common dashboard values

| iSolarCloud app / diagram | Likely sensor (code) | Notes |
|---|---|---|
| Solar Production | `total_active_power` or `inverter_ac_power` | Total AC power from the inverter(s). |
| Battery Usage | `total_field_energy_storage_active_power` (aliased as **Battery Power**) | Positive = charging, negative = discharging. |
| Battery Charge % | `battery_level_soc`, `battery_soc`, `total_field_soc`, or `energy_storage_soc_ems` | Depends on your system topology. |
| Load | `load_power` or `total_load_active_power` | Household consumption. |
| Usage from Grid | `grid_active_power` or `grid_active_power_ems` | Positive = importing, negative = exporting. |
| Daily Solar Yield | `daily_yield` / `inverter_daily_yield` / `daily_pv_yield_ems` | Resets at midnight local time. |
| Daily Feed-in | `feed_in_energy_today` / `daily_feed_in_energy_pv` | Energy exported to the grid today. |
| Daily Import | `energy_purchased_today` / `total_purchased_energy` | Energy imported from the grid today. |

## Battery-specific points

The following codes are surfaced for many hybrid inverters / battery systems:

- `total_field_energy_storage_active_power` → **Battery Power** (kW)
- `total_field_maximum_rechargeable_power` → **Battery Max Charge Power**
- `total_field_maximum_dischargeable_power` → **Battery Max Discharge Power**
- `total_field_chargeable_energy` → **Battery Chargeable Energy**
- `total_field_dischargeable_energy` → **Battery Dischargeable Energy**
- `daily_field_charge_capacity` → **Battery Daily Charge Capacity**
- `daily_field_discharge_capacity` → **Battery Daily Discharge Capacity**

If you need separate **Battery Charge Power** and **Battery Discharge Power** sensors (rather than a single signed value), you can request the additional point IDs via the integration options once you know them for your hardware. See the next section.

## Adding extra measure points

Some point IDs are not included in the default request because they vary by inverter model or firmware. The integration can request any point ID you provide:

1. Open **Settings → Devices & services → Sungrow iSolarCloud → Configure**.
2. In **Extra measure points**, enter a comma-separated list of `point_id=code` pairs, for example:
   ```
   99999=battery_charge_power, 99998=battery_discharge_power
   ```
3. The point IDs must be numeric; the codes can be any descriptive name you like.
4. Save — the integration will reload and create sensors for those codes.

You can find the actual point IDs for your hardware by:

- Checking the **Common Measuring Point Enumeration** in the iSolarCloud Developer Portal.
- Running a diagnostics dump from the device page in Home Assistant.
- Using community tools such as [GoSungrow](https://github.com/MickMake/GoSungrow) to list points for your plant.

### Recommended measure points (from the official docs)

The official iSolarCloud measuring-point catalogs — also served by the [mcp-isolarcloud](https://github.com/KRoperUK/mcp-isolarcloud) docs server — list the point IDs, names and units per device type. The integration ships a **grounded catalog of ~640 documented points** (all 17 device types), so pasting the matching `point_id=code` pairs into **Extra measure points** gives nicely-named, correctly-classified sensors.

**Classification is automatic.** Device and state class are inferred from the API-reported unit, and — new in this release — from the documented point when the API reports *no* unit. So the dimensionless points (SOC, SOH, power factor, performance ratio, charge/discharge cycle counts) now classify correctly instead of showing up as plain text, and status points (EV charger status, inverter operating state) render as human-readable text via a `SensorDeviceClass.ENUM`. Energy points feed the Energy dashboard automatically. The friendly name comes from the point ID even if you pick your own `code`, so you don't have to memorise the exact code.

**Battery** ([Common Battery Measuring Points](https://github.com/KRoperUK/mcp-isolarcloud)):
```
58604=battery_level, 58605=battery_soh, 58601=battery_voltage, 58602=battery_current, 58603=battery_temperature, 58606=battery_total_charge_energy, 58607=battery_total_discharge_energy
```

**EV charger** (Common Charger Measuring Points):
```
33708=ev_charger_power, 33723=ev_charger_max_power, 33716=ev_charger_status
```

**Energy meter** (Common Energy Meter Measuring Points):
```
8030=meter_forward_active_energy, 8031=meter_reverse_active_energy, 8062=meter_daily_forward_active_energy, 8063=meter_daily_reverse_active_energy, 8018=meter_active_power, 8014=meter_power_factor, 8026=meter_apparent_power, 8064=meter_frequency
```

**Energy storage inverter** (Common Energy Storage Inverter Measuring Points):
```
13126=battery_charge_power, 13150=battery_discharge_power, 13141=battery_soc, 13142=battery_soh, 13034=battery_total_charge_energy, 13035=battery_total_discharge_energy, 13119=load_power, 13121=feed_in_power, 13149=purchased_power, 13146=inverter_operating_status
```

**EMS device** (Common EMS Device Measuring Points):
```
24625=ems_storage_power, 24629=ems_storage_soc, 24626=ems_grid_power, 24624=ems_pv_power, 24631=ems_active_load, 24622=ems_total_charge, 24623=ems_total_discharge
```

**Microinverter** (Common Microinverter Measuring Points):
```
51303=micro_active_power, 51302=micro_total_yield, 51346=micro_yield_today, 51307=micro_power_factor, 51301=micro_running_status
```

Combiner-box, PCS, CMU/BSC, LC, environment-monitoring and communications catalogs are covered too — browse them via the docs server and add any `point_id=code` pair you need. Point IDs are consistent per device *type* but not guaranteed across every model/firmware, so confirm against your hardware if a point is missing.

## Dispatch / control entities

If your inverter / ESS supports parameter configuration, the integration also creates **Number** and **Select** entities per plant for dispatch control:

| Entity | Parameter | Range / options |
|---|---|---|
| Charge/Discharge Command | `charge_discharge_command` | Stop / Charge / Discharge |
| Charge/Discharge Power | `charge_discharge_power` | 0–5000 W |
| SOC Upper Limit | `soc_upper_limit` | 70–100 % |
| SOC Lower Limit | `soc_lower_limit` | 0–50 % |
| Forced Charging | `forced_charging` | Disable / Enable |
| Forced Charge Target SOC | `forced_charging_target_soc_1` | 0–100 % |

When you set **Charge/Discharge Command** to *Charge* or *Discharge*, the integration automatically starts sending the External EMS heartbeat (param `10017`) every 60 seconds so the inverter stays in dispatch mode. Selecting **Stop** turns the heartbeat off. If you remove the dispatch entities, the heartbeat is also stopped.

> Dispatch support requires the correct iSolarCloud API plan and firmware. The integration will only create dispatch entities if it can discover a compatible inverter or ESS device for the plant.

## EV charger support

If your EV charger appears as a separate device in iSolarCloud, request its points via the **Extra measure points** option (or enable **per-device sensors**, which polls each discovered device). The documented charger point IDs are listed under [Recommended measure points](#recommended-measure-points-from-the-official-docs) above; verified additions from other charger models are welcome.

## Still missing a sensor?

1. Enable debug logging for the integration:
   ```yaml
   logger:
     logs:
       custom_components.sungrow: debug
   ```
2. Reload the integration and look for the raw realtime data in the logs.
3. Find the point ID / code for the value you want and add it via the options, or open an issue with the redacted raw data.
