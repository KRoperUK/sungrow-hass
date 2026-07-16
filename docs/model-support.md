---
icon: lucide/table-2
---

# Model support matrix

Which sensors and controls you get depends on your **inverter family**. Sungrow expose
different measure points (and different point IDs) per model, so this page shows what to
expect for each family — and, when something is missing, whether that's a model limitation
or something to report.

The integration resolves the family automatically from the device's model code
(`device_model_code`, e.g. `SG3.6RS`, `SH10RT-20`), and uses it to request the right
points — for example the MPPT voltage/current range differs between string inverters and
hybrids. A model the resolver doesn't recognise still works: it falls back to the generic,
device-type behaviour, so nothing is lost — you just won't get the family-specific tuning.

## Families

| Family | Example models | Type | Battery |
|---|---|---|---|
| **SG · single-phase string** | SG3.0RS, SG3.6RS, SG5.0RS | PV string inverter | No |
| **SG · three-phase string** | SG8.0RT, SG10RT, SG110CX | PV string inverter | No |
| **SH · single-phase hybrid** | SH3.6RS, SH5.0RS, SH6.0RS | Hybrid / storage | Yes |
| **SH · three-phase hybrid** | SH8.0RT, SH10RT‑20, SH20T | Hybrid / storage | Yes |

Prefix `SG` = PV string inverter (no battery); `SH` = hybrid with a battery. Suffix `RS` =
single-phase; `RT` / `T` / `CX` = three-phase.

## What each family reports

| Capability | SG single-phase | SG three-phase | SH single-phase | SH three-phase |
|---|:--:|:--:|:--:|:--:|
| PV power / daily & total yield | ✅ | ✅ | ✅ | ✅ |
| MPPT voltage/current | ✅ (pts 5–10) | ✅ (pts 5–10) | ✅ (pts 13xxx) | ✅ (pts 13xxx) |
| Per-string DC V/I | ✅ | ✅ | ✅ | ✅ |
| Per-phase AC V/I | Phase A | A / B / C | Phase A | A / B / C |
| Battery SOC / power / health | — | — | ✅ | ✅ |
| Battery charge & discharge power | — | — | ✅ | ✅ |
| Grid meter (import/export) | ✅ | ✅ | ✅ | ✅ |
| Dispatch — grid/export limits | ✅ | ✅ | ✅ | ✅ |
| Dispatch — charge/discharge, SOC, forced-charge | — | — | ✅ | ✅ |

✅ = available where the API/model reports it. Points a specific model or firmware doesn't
return are simply skipped (no broken entities). Battery rows depend on a battery actually
being present — the dispatch battery controls are hidden on PV-only plants for safety.

## Data sources

Any model can be connected through one of several transports (see
[transport modes](local-modbus.md#transport-modes)): the official **developer Cloud** (OAuth,
full sensors + dispatch), an **unofficial user-account Cloud** login (email/password, read-only
plant-level data, [experimental](local-modbus.md#cloud-user-account-unofficial)), and **local
Modbus**. The matrix below describes what a model reports; which of those datapoints you actually
get also depends on the transport.

## Cloud vs local Modbus

Most points are available over both the cloud API and local Modbus (WiNet-S), but coverage
differs by family. See [Local Modbus](local-modbus.md) for setup.

| Family | Cloud API | Local Modbus (WiNet-S) |
|---|:--:|:--:|
| SG single-phase string | ✅ | ✅ (SG-RS register map) |
| SG three-phase string | ✅ | ⚠️ not yet mapped — see [#219](https://github.com/KRoperUK/sungrow-hass/issues/219) |
| SH single-phase hybrid | ✅ | ✅ (SH register map) |
| SH three-phase hybrid | ✅ | ✅ (SH register map) |

Local Modbus is currently **read-only**; dispatch/control still goes through the cloud
([#220](https://github.com/KRoperUK/sungrow-hass/issues/220)).

## Known model-specific caveats

- **SH20T — MPPT not exposed via cloud.** On some firmware the SH20T doesn't return MPPT
  voltage/current over the iSolarCloud API even with per-device sensors enabled, so those
  sensors won't appear (reported in
  [#189](https://github.com/KRoperUK/sungrow-hass/issues/189)). This is a data limitation of
  the model/firmware, not a bug in the integration.
- **SH10RT‑20 — battery charge/discharge power.** Separate **Battery Charging Power** and
  **Battery Discharging Power** sensors are requested automatically for hybrid inverters (no
  manual configuration; [#31](https://github.com/KRoperUK/sungrow-hass/issues/31)).
- **SG-RS — local `daily_yield`.** The daily-yield register read over Modbus can diverge from
  the cloud value on some SG-RS firmware; the integration derives daily yield from the
  trustworthy lifetime total instead ([#223](https://github.com/KRoperUK/sungrow-hass/issues/223)).

## Missing a sensor?

If a value you expect isn't appearing:

1. Check the table above — it may be a known limitation of your model.
2. Download a **diagnostics** dump from the device page. The `points_catalog` section lists
   **every point your hardware actually reports** (point ID, name, value, unit) per device —
   copy the IDs you want into **Extra measure points** (see
   [Adding extra measure points](SENSORS.md#adding-extra-measure-points)).
3. If a point *is* reported but not surfaced — or your model isn't recognised as the right
   family — please [open an issue](https://github.com/KRoperUK/sungrow-hass/issues) with the
   redacted diagnostics so the model map can be extended.

!!! info "Help us grow the matrix"
    This matrix is maintained from confirmed hardware reports. If your model behaves
    differently from what's shown here, a diagnostics dump on an issue lets us correct it and
    tune the per-model point requests for everyone with that model.
