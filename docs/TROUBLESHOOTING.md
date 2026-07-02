# Troubleshooting

This page covers the most common problems reported with the Sungrow iSolarCloud
integration. Please read it before opening an issue.

## Enable debug logging

Add this to your `configuration.yaml`, restart Home Assistant, and reproduce the
problem:

```yaml
logger:
  default: info
  logs:
    custom_components.sungrow: debug
    pysolarcloud: debug
```

When sharing logs, **redact your App Key, App Secret, and any tokens.**

---

## "Invalid authentication" / "Operation failed" during setup

These are almost always caused by the App ID, redirect URI, or API approval, not
by the integration itself.

1. **Use only the numeric App ID.** In the iSolarCloud developer portal the
   application URL looks like `.../editApplication?id=1234`. Enter **`1234`** in
   the *App ID* field — not the whole URL.
2. **Your API access must be approved by Sungrow.** New applications sit in
   *pending approval* and authorization will fail until approved. This commonly
   takes about a week. You cannot proceed before approval.
3. **Enable OAuth 2.0** for your application in the developer portal.
4. **The redirect URI must match exactly** in three places: the developer portal,
   the value you enter in Home Assistant, and what gets used during the token
   exchange. Scheme (`http`/`https`), host/IP, and path must all match.
   - Local: `http://homeassistant.local:8123/api/sungrow_hass/callback`
   - Nabu Casa: `https://<your-id>.ui.nabu.casa/api/sungrow_hass/callback`
5. **The hub is created first, then you authorize it.** After you submit your
   credentials Home Assistant creates the hub and prompts you to *authorize* it
   (a reconfigure/authorize notification on the integration). Creating the hub
   first registers the `/api/sungrow_hass/callback` endpoint, so the redirect
   resolves even on a brand-new install (this is the fix for the *404 on the
   callback during a first-time setup*). Open the prompt and a *Waiting for
   authorization* screen appears; log in and approve the app and the code is
   captured automatically.
6. **Manual entry is the fallback.** If the redirect does not complete (for
   example the callback endpoint returns an error, or iSolarCloud strips the
   `flow_id` query parameter), the screen automatically switches to an *Enter
   Authorization Code* form after a short wait. Paste the `code` value from the
   URL bar there — or paste the whole redirect URL and the integration extracts
   the code for you. Make sure the base redirect URI still matches the developer
   portal exactly.

---

## Entities go "unavailable" after restarting / updating Home Assistant

**This is handled automatically by current versions.** Very old versions
(pre-0.3.0) did not persist the rotated refresh token, so after a restart the
stored token was already invalid and every entity went unavailable — the only
workaround was to delete and re-add the integration.

The integration now:

- Saves refreshed tokens back to the config entry automatically, so they
  survive restarts.
- If the stored credentials ever do become invalid, Home Assistant shows a
  **"Reconfigure"/reauth** prompt for the integration. Click it and re-authorize
  — you no longer need to delete and re-add the integration or lose your entity
  history.

If you are on a pre-0.3.0 version, please update first.

---

## Sensors update too often / not often enough

The default polling interval is **5 minutes**. You can change it:

**Settings → Devices & Services → Sungrow → Configure → Polling interval.**

iSolarCloud typically allows ~2000 API calls/hour, so very low intervals across
many sensors can still be served, but a conservative interval is gentler on the
API and your account.

---

## The Energy dashboard can't use my sensors

The integration infers `device_class` and `state_class` from each sensor's unit (energy,
power, voltage, current, temperature, etc.). Energy sensors (`Wh`/`kWh`/`MWh`) are
exposed with `device_class: energy` and `state_class: total_increasing`, which the
Energy dashboard requires. If a specific sensor still isn't selectable, open an
issue with the sensor's unit and the `code` shown in its attributes.

---

## A device (EV charger, meter, etc.) isn't showing up

The plant realtime endpoint only returns the point IDs the integration knows to
ask for, so hardware the upstream library hasn't catalogued — e.g. a **Sungrow
AC011E EV charger** ([#18](https://github.com/KRoperUK/sungrow-hass/issues/18)) —
won't create sensors automatically even though it appears in the iSolarCloud app.

To help map it:

1. **Download diagnostics.** Go to **Settings → Devices & services → Sungrow
   iSolarCloud → ⋮ → Download diagnostics**. The JSON is redacted (no tokens or
   secrets) and now includes:
   - `all_devices` — every device on your plant, including the charger, with its
     `device_type` (unmapped hardware shows a raw numeric type).
   - `device_realtime` — a best-effort per-device realtime fetch for each device
     type, so any reachable charger points show up here.
2. **Attach that JSON to [#18](https://github.com/KRoperUK/sungrow-hass/issues/18)**
   so the point IDs can be added to the default mapping.
3. **Stopgap — surface points yourself.** If you already know a point ID (from the
   iSolarCloud Developer Portal's *Common Measuring Point Enumeration*), add it
   under **Configure → Extra measure points** as `point_id=code` (for example
   `<id>=ev_charger_power`). Recognised codes like `ev_charger_power` /
   `ev_charger_energy` get a friendly name automatically.

---

## Still stuck?

Open a [bug report](https://github.com/KRoperUK/sungrow-hass/issues/new/choose)
with your integration version, Home Assistant version, gateway region, and debug
logs (tokens redacted).
