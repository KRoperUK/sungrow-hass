# Sungrow iSolarCloud Integration for Home Assistant

[![HACS][hacs-badge]][hacs-url]
[![CI][ci-badge]][ci-url]
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
| **Gateway** | Your region (Europe, Australia, China, etc.) |
| **App Key** | AppKey from the [iSolarCloud Developer Platform](https://developer-api.isolarcloud.com/#/application) |
| **App Secret** | AppSecret from the Developer Platform |
| **App ID** | App ID — found in the Developer Platform URL: `…/editApplication?id=1234` |
| **Redirect URI** | Pre-filled; leave as default unless you know what you're doing |

4. Click **Submit** — you'll be shown an authorisation URL.
5. Visit the URL, log in, and paste the returned **code** back into Home Assistant.

### Obtaining Credentials

Register an application on the [iSolarCloud Developer Platform](https://developer-api.isolarcloud.com/#/application) to get your App Key, App Secret, and App ID.

> **Note:** New developer applications must be **approved by Sungrow** (and have **OAuth 2.0** enabled) before authorization will work — this can take up to a week.

### Options

After setup, go to **Settings → Devices & Services → Sungrow → Configure** to change:

- **Polling interval** (default 5 minutes)
- **Extra measure points** — add custom `point_id=code` pairs to request additional data points from iSolarCloud

### Sensor mapping

Not sure which sensor corresponds to which value in the iSolarCloud app? See [docs/SENSORS.md](docs/SENSORS.md).

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

## Support

Found a bug or have a feature request? [Open an issue][issues-url].

---

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[ci-badge]: https://github.com/KRoperUK/sungrow-hass/actions/workflows/ci.yml/badge.svg
[ci-url]: https://github.com/KRoperUK/sungrow-hass/actions/workflows/ci.yml
[release-badge]: https://img.shields.io/github/v/release/KRoperUK/sungrow-hass
[release-url]: https://github.com/KRoperUK/sungrow-hass/releases/latest
[hacs-my-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[hacs-my-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=KRoperUK&repository=sungrow-hass&category=integration
[issues-url]: https://github.com/KRoperUK/sungrow-hass/issues
