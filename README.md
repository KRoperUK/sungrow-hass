# Sungrow iSolarCloud Integration for Home Assistant

[![HACS][hacs-badge]][hacs-url]
[![CI][ci-badge]][ci-url]
[![codecov][codecov-badge]][codecov-url]
[![GitHub Release][release-badge]][release-url]

Custom component that integrates Sungrow inverters via the iSolarCloud API into Home Assistant using the [`sungrow-isolarcloud`](https://github.com/KRoperUK/pysolarcloud) library (a maintained fork of `pysolarcloud`).

## Features

- **Cloud Polling** — fetches real-time data from the iSolarCloud API.
- **Auto-Discovery** — automatically finds all plants linked to your account.
- **Sensors** — creates sensors for every available data point (power, energy, battery SOC, etc.) with correct device/state classes for the Energy dashboard.
- **Custom measure points** — request additional iSolarCloud point IDs (e.g. battery charge/discharge power or EV charger values) via the options flow.
- **Dispatch / control entities** — number and select entities for charge/discharge command, power, SOC limits, and forced charging, with automatic EMS heartbeat when dispatching.
- **Config Flow** — set up entirely through the Home Assistant UI.
- **Token persistence & re-auth** — refreshed tokens are saved automatically, so entities stay available across restarts; if credentials expire you're prompted to re-authorize in place (no delete & re-add).
- **Configurable polling interval** — tune how often data is fetched via the integration options.

## Use cases

Typical things people use this integration for:

- **Monitor your solar system in Home Assistant** — live generation, house consumption, grid import/export and battery state of charge as sensors, with the correct device/state classes so they feed straight into the **Energy dashboard** and long-term statistics.
- **Time-of-use battery control** — combine the dispatch **number**/**select** entities with automations to charge the battery from the grid when electricity is cheap and discharge it during peak-price periods (see [Example automations](#example-automations)).
- **Force-charge before high demand** — top the battery up ahead of a known heavy-usage window (e.g. before cooking or an EV charge session) using the forced-charging entities.
- **EV charger & meter visibility** — enable per-device sensors to surface EV charger power/energy and meter readings alongside the plant data.
- **Alerting** — notify on low battery SOC, a plant going unavailable, or a fault, using standard Home Assistant automations on the sensors this integration creates.

## Installation

### HACS (Recommended)

[![Open HACS Repository][hacs-my-badge]][hacs-my-url]

Or manually:

1. Open **HACS** → **Integrations**.
2. Click the three-dot menu → **Custom repositories**.
3. Add `https://github.com/KRoperUK/sungrow-hass` as an **Integration**.
4. Search for **Sungrow iSolarCloud** and install.
5. Restart Home Assistant.

### Manual

1. Download the [latest release][release-url].
2. Copy `custom_components/sungrow` into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.

## Configuration

1. Go to **Settings** → **Devices & Services**.
2. Click **Add Integration** and search for **Sungrow iSolarCloud**.
3. Enter your iSolarCloud API credentials:

| Field | Description |
|---|---|
| **Gateway** | Your region: **Europe**, **International**, **China**, or **Australia** |
| **App Key** | AppKey from the [iSolarCloud Developer Platform](https://developer-api.isolarcloud.com/#/application) |
| **App Secret** | AppSecret from the Developer Platform |
| **App ID** | App ID — found in the Developer Platform URL: `…/editApplication?id=1234` |
| **Redirect URI** | Pre-filled; leave as default unless you know what you're doing |

4. Click **Submit**. The **hub is created immediately** and Home Assistant prompts you to **authorize** it (shown as a "reconfigure/authorize" notification on the integration). Creating the hub first is what registers the callback endpoint, so the redirect resolves reliably even on a brand-new install.
5. Open the authorization prompt → a **"Waiting for authorization"** screen appears with a link. Open the link, log in to iSolarCloud, and approve the application. You're redirected back and Home Assistant captures the authorization code automatically (via the `/api/sungrow_hass/callback` endpoint) and finishes — no copy-and-paste needed.
6. If the automatic redirect doesn't complete within a couple of minutes (for example because iSolarCloud strips query parameters from your redirect URI), the screen falls back to a **manual entry** form. Paste the `code` from the redirect URL — or the whole redirect URL — to finish authorizing the hub.

### Obtaining Credentials

Register an application on the [iSolarCloud Developer Platform](https://developer-api.isolarcloud.com/#/application) to get your App Key, App Secret, and App ID.

> **Note:** New developer applications must be **approved by Sungrow** (and have **OAuth 2.0** enabled) before authorization will work — this can take up to a week.

### Options

After setup, go to **Settings → Devices & Services → Sungrow → Configure** to change:

- **Polling interval** (default 5 minutes)
- **Extra measure points** — add custom `point_id=code` pairs to request additional data points from iSolarCloud
- **Create per-device sensors** — off by default. When enabled, each discovered device (EV charger, meter, extra battery) is polled for its own realtime points and exposed as sensors under its own device card, grouped beneath the plant. Combine with **Extra measure points** to surface device-specific point IDs (e.g. an EV charger). Adds extra API calls, so leave it off if you only need plant-level data.

### Changing region or credentials

Picked the wrong gateway region, or rotated your API secret? Use **Settings →
Devices & Services → Sungrow → ⋮ → Reconfigure** to update the region, App Key,
App Secret, or redirect URI without deleting and re-adding the integration (your
entity history is kept). You'll be asked to authorize again in the browser, since
new credentials or a new region need fresh tokens. The App ID stays fixed.

### Removing the integration

Go to **Settings → Devices & Services → Sungrow iSolarCloud → ⋮ → Delete**. This
removes the config entry and all of its entities and devices; no files are left
behind (uninstall the repository from HACS separately if you also want to remove
the code). Your iSolarCloud account and developer application are unaffected.

### Sensor mapping

Not sure which sensor corresponds to which value in the iSolarCloud app? See [docs/SENSORS.md](docs/SENSORS.md).

## How data updates

This is a **cloud-polling** integration — it does not talk to the inverter locally.

- **Polling.** Home Assistant polls the iSolarCloud API on a fixed interval using one data update coordinator **per plant**. Every sensor for a plant refreshes together on each poll.
- **Interval.** The default is **5 minutes**. Change it under **Configure → Polling interval** (minimum 10 seconds). Lower intervals update sooner but use more of your API quota (the free developer plan allows ~2000 calls/hour); enabling per-device sensors adds a call per device type each poll.
- **Availability.** Entity state reflects the last successful poll. If a poll fails (network/API outage), entities go **unavailable** and Home Assistant retries on the next interval — no restart needed.
- **Authentication.** Access tokens are refreshed automatically and the rotated tokens are persisted, so entities stay available across restarts. If your credentials are revoked or expire, the integration triggers a **re-authorization** prompt rather than silently failing.
- **Dispatch controls are write-only.** The dispatch **number**/**select** entities send commands to the inverter; iSolarCloud does not report their current value back, so they act as controls (their state is not polled). While actively charging/discharging, the integration keeps the inverter in External EMS mode via a background heartbeat.

## Example automations

Entity IDs depend on your device name — check **Settings → Devices & Services → Sungrow** for the exact IDs. The examples below assume a device called `inverter`.

**Charge the battery overnight (cheap tariff), then let it discharge during the peak:**

```yaml
automation:
  - alias: "Battery: charge overnight"
    triggers:
      - trigger: time
        at: "00:30:00"
    actions:
      - action: select.select_option
        target:
          entity_id: select.inverter_charge_discharge_command
        data:
          option: "Charge"
      - action: number.set_value
        target:
          entity_id: number.inverter_charge_discharge_power
        data:
          value: 3000

  - alias: "Battery: stop forced charge at peak start"
    triggers:
      - trigger: time
        at: "05:30:00"
    actions:
      - action: select.select_option
        target:
          entity_id: select.inverter_charge_discharge_command
        data:
          option: "Stop"
```

**Notify when the battery gets low:**

```yaml
automation:
  - alias: "Battery: low SOC alert"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.my_plant_battery_state_of_charge
        below: 15
    actions:
      - action: notify.notify
        data:
          message: "Home battery is below 15%."
```

> Selecting **Charge**/**Discharge** (or setting charge/discharge power) automatically starts the EMS heartbeat; selecting **Stop** ends it.

## Supported devices & regions

- **Regions / gateways:** Europe, International, China, and Australia. Pick the one matching the account you registered your developer application under.
- **Devices:** grid-tied inverters, hybrid inverters, and energy storage systems (ESS / batteries) that appear in your iSolarCloud account. Sensors are created for whatever data points iSolarCloud returns for your plant.
- **Dispatch / control:** number and select entities (charge/discharge command & power, SOC limits, forced charging) are created for inverter / ESS devices that support External EMS control.

## Limitations

- **Cloud-only.** All data comes from the iSolarCloud cloud API — there is no local polling. If iSolarCloud is unreachable, or your account/plan loses API access, entities become unavailable.
- **API quota.** The free developer plan allows ~2000 calls/hour; keep the polling interval sensible, especially with many sensors.
- **Dispatch needs compatible firmware and plan.** External EMS / parameter setting requires an inverter and iSolarCloud plan that permit it. Where unsupported, dispatch entities may error on write.
- **Developer app approval.** New iSolarCloud developer applications must be approved by Sungrow (with OAuth 2.0 enabled) before authorization works — this can take up to a week.

## Troubleshooting

Having trouble authorizing, or entities showing as *unavailable*? See the
[Troubleshooting guide](docs/TROUBLESHOOTING.md) — it covers the common
"Invalid authentication" / "Operation failed" setup errors and the
unavailable-after-reboot problem.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for full developer setup and guidelines.

### Running Tests

```bash
pip install -r requirements_test.txt
ruff check custom_components/
ruff format --check custom_components/
pytest
```

### Live Integration Testing

To run live tests against the real iSolarCloud API:

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```env
   SUNGROW_APPKEY="your_app_key"
   SUNGROW_APPSECRET="your_app_secret"
   SUNGROW_APP_ID="your_app_id"
   ```

2. Run the live tests:
   ```bash
   pytest -m live
   ```

   > Live tests are automatically skipped when credentials are not set.

### Logo & icon

The integration ships `icon.png` / `logo.png` under `custom_components/sungrow/`.
For the brand images to appear in the Home Assistant UI's official brand system
(and on [brands.home-assistant.io](https://brands.home-assistant.io)), the
`sungrow` domain also needs a PR to the
[home-assistant/brands](https://github.com/home-assistant/brands) repository.

### Security

Found a security issue? Please follow the disclosure process in
[SECURITY.md](SECURITY.md) rather than opening a public issue.

## Support

Found a bug or have a feature request? [Open an issue][issues-url].

---

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[ci-badge]: https://github.com/KRoperUK/sungrow-hass/actions/workflows/ci.yml/badge.svg
[ci-url]: https://github.com/KRoperUK/sungrow-hass/actions/workflows/ci.yml
[codecov-badge]: https://codecov.io/gh/KRoperUK/sungrow-hass/branch/main/graph/badge.svg
[codecov-url]: https://codecov.io/gh/KRoperUK/sungrow-hass
[release-badge]: https://img.shields.io/github/v/release/KRoperUK/sungrow-hass
[release-url]: https://github.com/KRoperUK/sungrow-hass/releases/latest
[hacs-my-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[hacs-my-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=KRoperUK&repository=sungrow-hass&category=integration
[issues-url]: https://github.com/KRoperUK/sungrow-hass/issues
