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

If your EV charger appears as a separate device in iSolarCloud, its point IDs can be requested via the **Extra measure points** option once identified. The integration does not yet auto-discover EV charger devices because the available codes are not consistent across charger models; community contributions of verified point ID lists are welcome.

## Still missing a sensor?

1. Enable debug logging for the integration:
   ```yaml
   logger:
     logs:
       custom_components.sungrow: debug
   ```
2. Reload the integration and look for the raw realtime data in the logs.
3. Find the point ID / code for the value you want and add it via the options, or open an issue with the redacted raw data.
