# Sungrow iSolarCloud Integration for Home Assistant (API v1)

[![HACS][hacs-badge]][hacs-url]
[![CI][ci-badge]][ci-url]
[![GitHub Release][release-badge]][release-url]

Custom component that integrates Sungrow inverters via the **iSolarCloud API v1** (non-OAuth, username/password) into Home Assistant.

> **Note:** This version uses iSolarCloud API **v1** which authenticates with your username and password — no OAuth redirect flow required. If you need the OAuth-based v2 integration, see the [original repository](https://github.com/KRoperUK/sungrow-hass).

## Features

- **API v1 (non-OAuth)** — simple username/password authentication, no redirect flow needed.
- **Cloud Polling** — fetches real-time data from the iSolarCloud API every 5 minutes.
- **Auto-Discovery** — automatically finds all plants and devices linked to your account.
- **Sensors** — creates sensors for every available data point (power, energy, battery SOC, etc.).
- **Auto Re-login** — automatically refreshes the session token when it expires.
- **Config Flow** — set up entirely through the Home Assistant UI.

## Installation

### HACS (Recommended)

[![Open HACS Repository][hacs-my-badge]][hacs-my-url]

Or manually:

1. Open **HACS** → **Integrations**.
2. Click the three-dot menu → **Custom repositories**.
3. Add this repository URL as an **Integration**.
4. Search for **Sungrow iSolarCloud (API v1)** and install.
5. Restart Home Assistant.

### Manual

1. Download the [latest release][release-url].
2. Copy `custom_components/sungrow` into your Home Assistant `custom_components` directory.
3. Restart Home Assistant.

## Configuration

1. Go to **Settings** → **Devices & Services**.
2. Click **Add Integration** and search for **Sungrow iSolarCloud (API v1)**.
3. Enter your credentials:

| Field | Description |
|---|---|
| **AppKey** | AppKey from the [iSolarCloud Developer Platform](https://developer-api.isolarcloud.com/#/application) |
| **App Secret** | App Secret (x-access-key) from the Developer Platform |
| **Username / Email** | Your iSolarCloud account email or username |
| **Password** | Your iSolarCloud account password |
| **Gateway Region** | Your region (Europe, Australia, China, International) |

4. Click **Submit** — the integration will log in and create sensors for all your plants.

### Obtaining API Credentials

1. Go to the [iSolarCloud Developer Platform](https://developer-api.isolarcloud.com/#/application).
2. Register / log in and create an application.
3. **Important:** When creating the app, select **V1 (non-OAuth)** access.
4. Wait for approval (may take a few days).
5. Once approved, note your **AppKey** and **App Secret**.

## API Endpoints Used

This integration calls the following iSolarCloud OpenAPI v1 endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /openapi/login` | Authenticate and obtain session token |
| `POST /openapi/getPowerStationList` | List all plants on the account |
| `POST /openapi/getDeviceList` | List devices for a specific plant |
| `POST /openapi/getDeviceRealTimeData` | Fetch real-time data points for devices |

All requests include the `x-access-key` header (App Secret) and `appkey` in the body.

## Development

### Running Tests

```bash
pip install -r requirements_test.txt
pytest
```

### Live Integration Testing

To run live tests against the real iSolarCloud API:

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```env
   SUNGROW_APPKEY="your_app_key"
   SUNGROW_APPSECRET="your_app_secret"
   SUNGROW_USERNAME="your_email"
   SUNGROW_PASSWORD="your_password"
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
