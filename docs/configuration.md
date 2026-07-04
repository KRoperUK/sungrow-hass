---
icon: lucide/settings
---

# Configuration

Most options are reached from the integration entry: **Settings → Devices & Services → Sungrow
iSolarCloud → Configure**.

## Polling interval

The integration polls the iSolarCloud API on a fixed interval (default: 5 minutes). Lower it for
fresher data or raise it to stay within the API's rate limits.

!!! tip "Rate limits"
    iSolarCloud enforces hourly and monthly call quotas. If you see the integration retrying with
    a rate-limit message in the logs, **increase the polling interval** to make fewer calls.

## Custom measure points

The cloud returns a broad catalogue of measure points, but only the ones your hardware actually
reports produce a value. If you need an **additional point ID** that isn't surfaced by default
(for example a specific battery, meter, or EV-charger metric), add it under **Custom measure
points** in the options — provide the numeric iSolarCloud point ID and it will appear as a sensor
once it returns data.

!!! note "Unknown/absent sensors"
    Points for hardware you don't have (battery, EMS, EV charger on a PV-only system) return no
    reading and are **not created** as entities. If a point starts reporting later, its sensor is
    added automatically on the next poll.

## Dispatch / control entities

For inverters that support it, the integration can expose **number** and **select** entities to
control the battery and dispatch behaviour, such as:

- Charge / discharge command and power
- SOC upper / lower limits
- Forced charging schedules
- Energy-management / external-dispatch mode

When you write a dispatch value, the integration automatically sends the required **EMS
heartbeat** so the setting is applied and maintained.

!!! warning "Battery systems only"
    Dispatch controls only apply to inverters with an energy-storage system. On a PV-only plant
    they have no effect.

## Per-device sensors

By default, sensors are grouped at the plant level. You can optionally enable **per-device
sensors** to surface points reported by individual devices (e.g. an EV charger or a second
battery) under their own device in Home Assistant.

## Energy dashboard

Sensors are classified with the correct `device_class` / `state_class`, so energy points (Wh/kWh,
`device_class: energy`) can be added directly to the **Energy dashboard**.

!!! info "Battery State of Charge"
    Battery **State of Charge** is a percentage (`device_class: battery`), not an energy value, so
    it can't be added to the Energy dashboard's battery section. For that section, use the battery
    **charge/discharge energy** sensors (in Wh/kWh) instead.
