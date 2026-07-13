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
    iSolarCloud enforces hourly and monthly call quotas. If the quota is exceeded (E998/E999), the
    integration **automatically backs off** — doubling the effective interval up to a 1-hour cap —
    and raises a Home Assistant **Repair** (Settings → System → Repairs). To fix the root cause,
    **increase the polling interval** so fewer calls are made; the integration returns to your
    configured interval once the quota recovers.

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

When you set charge/discharge to *Charge* or *Discharge*, the integration switches
**Energy Management Mode** to Compulsory (Forced) so the inverter actually follows the
command, and sends the required **EMS heartbeat** so the setting is maintained. *Stop*
restores Self-consumption mode.

!!! warning "Battery controls are hidden on PV-only plants"
    Battery dispatch controls (charge/discharge command & power, SOC limits, forced charging,
    battery-first mode) are only created when the plant has a battery/ESS device. On a **PV-only**
    plant they are **hidden entirely** — commanding charge/discharge on a battery-less inverter can
    force it into External-EMS mode and suppress generation. Export- and active-power-limiting
    controls remain available.

## Per-device sensors

Plant readings are already grouped under the physical device they come from — inverter, battery,
meter or WiNet-S — nested beneath the plant (see [Sensors → Device grouping](SENSORS.md#device-grouping)).
On top of that, you can optionally enable **per-device sensors** to fetch points reported *only* by
an individual device (e.g. an EV charger or a second battery) and expose them under that device.
Enabling this also surfaces the documented **diagnostic** points per device type — inverter
temperature / MPPT voltages & currents, battery health (voltage, current, temperature, SOH), and
WiNet-S WLAN/wireless signal strength.

Regardless of this option, every device gets a **Fault** (problem) and **Connectivity**
(online/offline) binary sensor, and its device card is enriched with model, serial number and
manufacturer. The Fault sensor exposes an `operating_status` attribute with a human-readable
reason for inverter/ESS devices (e.g. *Shut down due to faults*, *Low insulation resistance*),
and the Connectivity sensor exposes the commissioning date as an attribute.

## Local Modbus (separate entry)

Local WiNet-S Modbus is a **separate integration entry**, not a field on the cloud options.
Discover the dongle (or import a local entry) to get independent local sensors with their own
poll interval. Cloud stays pure iSolarCloud. When serials match, the local inverter device is
nested under the cloud plant in the device registry without merging values. See
[Local Modbus (WiNet-S)](local-modbus.md).

## Energy dashboard

Sensors are classified with the correct `device_class` / `state_class`, so energy points (Wh/kWh,
`device_class: energy`) can be added directly to the **Energy dashboard**.

!!! info "Battery State of Charge"
    Battery **State of Charge** is a percentage (`device_class: battery`), not an energy value, so
    it can't be added to the Energy dashboard's battery section. For that section, use the battery
    **charge/discharge energy** sensors (in Wh/kWh) instead.
