"""Shared helper functions for the Sungrow config flow (#354).

Free functions with no dependency on the flow class or ``hass`` instance — kept
here so the per-transport modules and the options flow can share them without
importing each other or the shell class.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from ..oauth_view import OAUTH_CALLBACK_PATH

# How long to wait for the OAuth redirect before offering (or, on the manual step,
# giving up on) automatic completion. Generous, since iSolarCloud's approval page
# can be slow — a redirect that lands within this window completes the flow
# automatically even if the user has already reached the manual-entry form.
CALLBACK_WAIT_TIMEOUT = 300


def _normalize_redirect_uri(raw: str | None) -> str | None:
    """Return a redirect URI whose path is the OAuth callback, or ``None`` if unfixable.

    iSolarCloud must redirect the user back to Home Assistant's OAuth callback view,
    which is registered at :data:`OAUTH_CALLBACK_PATH` (``/api/sungrow_hass/callback``).
    Users occasionally paste just their Home Assistant base URL into the ``redirect_uri``
    field (e.g. ``http://192.168.0.218:8123``), which sends iSolarCloud to the HA
    homepage instead of the callback endpoint and silently drops the ``code`` query
    parameter (#340).

    Rules applied here:

    - Whitespace is stripped.
    - Trailing slashes on the base URL are collapsed.
    - If the URI already ends with :data:`OAUTH_CALLBACK_PATH`, it is returned as-is.
    - If it looks like a bare base URL (has a scheme and host, no path or just ``/``),
      the callback path is appended.
    - Anything else — a URI with an *incorrect* non-empty path, a value missing the
      scheme, or an empty string — is refused by returning ``None`` so the caller can
      surface a clear validation error rather than silently sending iSolarCloud a
      URI that would misroute the callback.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    # Require a scheme so we never silently accept ``example.com`` (no scheme -> Sungrow
    # would reject the redirect anyway, but the failure would be far from the input).
    if "://" not in text:
        return None
    if text.endswith(OAUTH_CALLBACK_PATH):
        return text
    # Split scheme + rest, then split host + path.
    scheme, rest = text.split("://", 1)
    if "/" in rest:
        host, path = rest.split("/", 1)
        path = "/" + path
    else:
        host = rest
        path = ""
    # Accept bare base URLs (``http://host:port`` or ``http://host:port/``) and auto-
    # append the callback path. Reject anything with a non-empty, non-slash path that
    # doesn't end in the callback — that's a mis-configuration a user should fix
    # deliberately, not one we should paper over.
    if path in ("", "/"):
        return f"{scheme}://{host}{OAUTH_CALLBACK_PATH}"
    return None


def _parse_winet_properties(props: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract ``(inverter_serial, model)`` from a WiNet-S mDNS TXT-record dict.

    The dongle advertises ``inverter=1;<type_code>;<serial>;1;<x>;<model>;...``. TXT
    values may arrive as ``bytes``; a missing/short field yields ``None``.
    """
    raw: Any = props.get("inverter")
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if not raw:
        return None, None
    parts = str(raw).split(";")
    serial = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    model = parts[5].strip() if len(parts) > 5 and parts[5].strip() else None
    return serial, model


def _parse_extra_measure_points(raw: str | None) -> dict[str, str]:
    """Parse a comma-separated 'point_id=code' list into a mapping.

    Whitespace around entries is ignored; duplicate point_ids keep the last value.
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise vol.Invalid(f"Extra measure point '{entry}' must be in the form point_id=code")
        point_id, code = entry.split("=", 1)
        point_id = point_id.strip()
        code = code.strip()
        if not point_id or not code:
            raise vol.Invalid("point_id and code must not be empty")
        if not point_id.isdigit():
            raise vol.Invalid(f"point_id must be numeric, got '{point_id}'")
        out[point_id] = code
    return out
