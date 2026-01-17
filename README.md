<!-- ![Logo](images/logo.svg) -->

# Sungrow iSolarCloud Home Assistant Integration

This custom component integrates Sungrow inverters via iSolarCloud into Home Assistant. It uses the `pysolarcloud` library to communicate with the iSolarCloud API.

## Features

-   **Cloud Polling**: Fetches data from iSolarCloud API.
-   **Auto-Discovery**: Automatically discovers plants linked to your account.
-   **Sensors**: Creates sensors for all available data points (Power, Energy, Battery SOC, etc.).
-   **Config Flow**: Easy setup via Home Assistant UI.

## Installation

### HACS (Recommended)

1.  Open HACS in Home Assistant.
2.  Go to "Integrations".
3.  Click the three dots in the top right corner and select "Custom repositories".
4.  Add `https://github.com/KRoperUK/sungrow-hass` as an "Integration".
5.  Search for "Sungrow iSolarCloud" and install it.
6.  Restart Home Assistant.

### Manual Installation

1.  Download the latest release.
2.  Copy the `custom_components/sungrow_hass` folder to your Home Assistant `custom_components` directory.
3.  Restart Home Assistant.

## Configuration

1.  Go to **Settings** -> **Devices & Services**.
2.  Click **Add Integration** and search for "Sungrow iSolarCloud".
3.  Enter your iSolarCloud credentials:
    -   **Gateway**: Select your region (e.g., Europe, Australia, China).
    -   **App Key**: Your AccessKey from iSolarCloud.
    -   **App Secret**: Your AccessKey Secret from iSolarCloud.
    -   **App ID**: Your App ID (usually `2190` for the iSolarCloud App).

### Obtaining Credentials

To get your App Key and Secret, you might need to request them from Sungrow or use the default App ID `2190` which mimics the mobile app.

## Support

If you encounter any issues, please open an issue on the [GitHub repository](https://github.com/KRoperUK/sungrow-hass/issues).
