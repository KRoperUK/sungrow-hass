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
    integration **automatically backs off** — using the delay iSolarCloud suggests when it gives
    one, otherwise doubling the effective interval, up to a 1-hour cap — and raises a Home
    Assistant **Repair** (Settings → System → Repairs). To fix the root cause,
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

### Scheduled charge / discharge windows

Each window has a start, an end, a battery mode, and the **days of the week** it runs on.
Leave all seven selected for a window that runs every day — that stores no mask at all, which
is how windows behaved before day selection existed. Clearing every day is refused: a window
that can never run is a misconfiguration, not a disabled slot (clear the start and end times
to disable a slot instead).

A start or end is either a fixed **local** time (`HH:MM`) or **sun-relative**: `sunrise` or
`sunset`, optionally with an offset of up to ±12 hours — `sunset-00:30`, `sunrise+01:00`.
The two kinds can be mixed in one window (`22:00 → sunrise`).

A fixed-time window whose end is earlier than its start wraps past midnight and belongs to
the day it *starts* on, so a Monday-only `23:30 → 06:00` also covers Tuesday morning up to
06:00, and then stays off until the following Monday.

#### Sunrise / sunset-relative windows

Sun times are calculated for the location and time zone set in Home Assistant
(*Settings → System → General*), for each calendar day, using the same astral library Home
Assistant's own sun features use. They follow the season automatically — a
`sunset-00:30 → sunset+03:00` discharge tracks the evening peak all year. How the edge cases
behave:

- **Offsets are elapsed time.** `sunset+03:00` is three real hours after sunset, even if the
  clocks change in between.
- **A boundary belongs to its sun event's day.** `sunset+03:00` may fall after midnight (in a
  UK summer it is around 00:20); it still belongs to the day of that sunset, so a Sunday-only
  `sunset-00:30 → sunset+03:00` runs from Sunday evening into the small hours of Monday.
- **Overnight or not is decided by the window, not the season.** To tell whether the end
  belongs to the next day, sunrise counts as 06:00 and sunset as 18:00 (plus any offset):
  `22:00 → sunrise` and `sunset → sunrise` are overnight windows; `sunrise → sunset` and
  `sunset → 23:00` are not.
- **Days where the times don't fit are skipped, with a warning in the log.** If on a given
  day the end resolves to at or before the start — `sunset → 21:00` in a UK midsummer, when
  sunset is about 21:20 — the window does not run that day, rather than stretching to almost
  24 hours.
- **No sunrise or sunset, no window.** Inside the polar circles, on days the sun never rises
  or never sets, a window with that boundary does not run that day (logged once per day).
  There is no fixed-time fallback.
- **Rejected when saving:** offsets beyond ±12 hours, unknown words, and windows anchored to
  the same event whose end offset is not after the start offset (`sunset+01:00 → sunset`).
- **Restarts are safe.** On start-up the active window is worked out from that day's resolved
  times and its mode applied once; boundaries already passed are not replayed.
- Overlaps follow the same rule as fixed windows: the window that starts latest (by clock
  time) wins.

Outside every window the battery returns to Self-consumption, so a schedule never leaves the
inverter stuck in a forced mode.

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
