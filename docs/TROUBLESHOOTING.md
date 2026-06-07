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
5. After agreeing to authorize you may land on a **404 page** — that's expected.
   Copy the `code` value from the URL bar (or paste the whole URL; the integration
   extracts the code for you).

---

## Entities go "unavailable" after restarting / updating Home Assistant

**This is fixed in v0.3.0+.** Earlier versions did not persist the rotated
refresh token, so after a restart the stored token was already invalid and every
entity went unavailable — the only workaround was to delete and re-add the
integration.

As of v0.3.0:

- Refreshed tokens are saved back to the config entry automatically, so they
  survive restarts.
- If the stored credentials ever do become invalid, Home Assistant shows a
  **"Reconfigure"/reauth** prompt for the integration. Click it and re-authorize
  — you no longer need to delete and re-add the integration or lose your entity
  history.

If you are on an older version, please update first.

---

## Sensors update too often / not often enough

The default polling interval is **5 minutes**. You can change it:

**Settings → Devices & Services → Sungrow → Configure → Polling interval.**

iSolarCloud typically allows ~2000 API calls/hour, so very low intervals across
many sensors can still be served, but a conservative interval is gentler on the
API and your account.

---

## The Energy dashboard can't use my sensors

v0.3.0+ infers `device_class` and `state_class` from each sensor's unit (energy,
power, voltage, current, temperature, etc.). Energy sensors (`Wh`/`kWh`/`MWh`) are
exposed with `device_class: energy` and `state_class: total_increasing`, which the
Energy dashboard requires. If a specific sensor still isn't selectable, open an
issue with the sensor's unit and the `code` shown in its attributes.

---

## Still stuck?

Open a [bug report](https://github.com/KRoperUK/sungrow-hass/issues/new/choose)
with your integration version, Home Assistant version, gateway region, and debug
logs (tokens redacted).
