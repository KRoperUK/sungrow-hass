---
icon: lucide/arrow-up-right
---

# Upgrading

Changes that need action on your side, per major version. Minor and patch releases
are always safe to install without reading anything in advance — see the
[GitHub releases](https://github.com/KRoperUK/sungrow-hass/releases) page for the
full per-release changelog.

## v5.x → v6.0

Two changes to be aware of before upgrading. Neither requires re-adding the
integration or re-authorizing.

### Automations comparing to the numeric state of `running_state_raw` or `device_type_code` will break

**Affects:** cloud entries with per-device sensors enabled, and every local Modbus entry.

Since [#325](https://github.com/KRoperUK/sungrow-hass/issues/325), those two sensors are
classified as `enum` and expose the **decoded label** (`"Running"`, `"Standby"`,
`"Emergency Stop"`, `"SG3.6RS"`, `"SH10RT"`, …) instead of the raw integer code they
used to hold. This makes them useful on dashboards and in the Logbook, but it means an
automation like this no longer matches:

```yaml
# BEFORE (v5.x) — stops working after upgrade
- trigger: state
  entity_id: sensor.my_inverter_running_state_raw
  to: "4"
```

Change the comparison to the enum label:

```yaml
# AFTER (v6.x)
- trigger: state
  entity_id: sensor.my_inverter_running_state_raw
  to: "Emergency Stop"
```

The label set is the values shown on the [Sensors](SENSORS.md) page for each enum
sensor. Any automation that used the raw integer is worth checking after upgrade — HA
won't warn you: it just silently stops firing.

!!! tip "Finding stale references"
    Search your automations and dashboards for `running_state_raw`, `device_type_code`
    (and the older `inverter_state` alias if you used it). Anything comparing to a
    numeric-looking string on those entities needs the enum label instead.

### SBH cloud plants — battery State of Charge is now a proper percentage

**Affects:** cloud entries for SBH (residential battery / hybrid) plants where SOC
previously reported values in the `0.00 – 1.00` range instead of `0 – 100`.

Since [#228](https://github.com/KRoperUK/sungrow-hass/issues/228), the integration
scales those SOC readings to a proper 0-100 percentage server-side. If you added a
manual **`* 100`** in a template sensor, automation or Lovelace card to work around
the old value, remove it — otherwise your value now reads e.g. `4200` when the
battery is at 42%.

```yaml
# BEFORE (v5.x) — remove after upgrade
template:
  - sensor:
      - name: "Battery SOC (percent)"
        state: "{{ states('sensor.plant_battery_soc') | float * 100 }}"

# AFTER (v6.x) — read the sensor directly
# sensor.plant_battery_soc already reports 0-100
```

Long-term statistics for these entities are **not** rewritten on upgrade — the old
0-1 values stay in the historical series. If your Energy dashboard graph has a step
change at the upgrade time, that's why. Use the `sungrow.backfill` service to
re-import the affected period if you want a clean history.

### Also worth knowing

Not breaking (no user action required), but shape-changing enough to mention:

- **`cloud_modbus` transport retired.** Entries that used the pre-v5 hybrid transport
  are **auto-migrated on load** — the `modbus_host` value is stripped from the cloud
  entry, and if the inverter serial is known, a **separate local Modbus entry** is
  created automatically alongside it. No user action needed, but the shape of what
  used to be one entry is now two. See
  [Upgrading from hybrid](local-modbus.md#upgrading-from-hybrid-old-modbus-host-on-cloud).
- **Charge/Discharge Command select replaced by Battery Mode.** The old
  `select.*_charge_discharge_command` entity has been superseded by
  `select.*_battery_mode` with a richer option set (Self-consumption / Force charge /
  Force discharge / Stop). Restored *Charge* / *Discharge* / *Stop* states from the
  old entity map onto the new options, so existing automations keep working; new
  automations should use the new entity or the `sungrow.set_battery_mode` service.
