"""Shared helper functions for the Sungrow config flow (#354).

Free functions with no dependency on the flow class or ``hass`` instance — kept
here so the per-transport modules and the options flow can share them without
importing each other or the shell class.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from ..oauth_view import OAUTH_CALLBACK_PATH

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# How long to wait for the OAuth redirect before offering (or, on the manual step,
# giving up on) automatic completion. Generous, since iSolarCloud's approval page
# can be slow — a redirect that lands within this window completes the flow
# automatically even if the user has already reached the manual-entry form.
CALLBACK_WAIT_TIMEOUT = 300

# Zeroconf browse window used by the local-Modbus discovery wizard step. WiNet-S
# dongles advertise on ``_http._tcp.local.``; three seconds is enough for the
# HaZeroconf cache to hand us any live dongle without keeping the user on a
# spinner for longer than necessary.
DISCOVERY_BROWSE_TIMEOUT = 3.0


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


@dataclass(frozen=True)
class WinetDongle:
    """A single WiNet-S dongle discovered by :func:`async_discover_winet_dongles`.

    ``host`` is the reachable IPv4 address (as a string) the config flow can hand to
    Modbus. ``serial`` and ``model`` come from the dongle's ``inverter`` TXT record
    (see :func:`_parse_winet_properties`) — either may be ``None`` when the record is
    absent or truncated, which is fine: the identify step later reads the same fields
    from Modbus directly. ``mdns_name`` is the fully-qualified mDNS instance name so
    tests and diagnostics can distinguish two dongles that share a serial (rare, but
    possible with cloned firmware in a lab).
    """

    host: str
    serial: str | None
    model: str | None
    mdns_name: str | None


async def async_discover_winet_dongles(
    hass: HomeAssistant, *, timeout: float = DISCOVERY_BROWSE_TIMEOUT
) -> list[WinetDongle]:
    """Actively scan the LAN for WiNet-S dongles via HA's shared zeroconf instance.

    Runs an ephemeral :class:`AsyncServiceBrowser` on ``_http._tcp.local.`` for
    ``timeout`` seconds, filters for instance names starting with ``WiNet-WebServer``
    (case-insensitive — Sungrow ships mixed case), resolves each hit and returns a
    :class:`WinetDongle` per unique IPv4 record. On any zeroconf error, or when the
    integration is running in a test environment without zeroconf, returns an empty
    list so the caller can fall back to the manual IP step without a hard failure.

    The browser is cancelled before this function returns, so calling it repeatedly
    (e.g. from a wizard "Rescan" action) does not leak background tasks.
    """
    try:
        from homeassistant.components import zeroconf as ha_zc
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
    except ImportError:
        _LOGGER.debug("zeroconf not installed; local-modbus discovery unavailable")
        return []

    try:
        aiozc = await ha_zc.async_get_async_instance(hass)
    except Exception as err:  # pylint: disable=broad-except
        # In tests or when the zeroconf integration is disabled the instance getter
        # can raise. Fall back to "no discovery" rather than propagating, so the
        # manual IP path stays reachable.
        _LOGGER.debug("could not obtain zeroconf instance: %s", err)
        return []

    zc = aiozc.zeroconf
    seen_names: set[str] = set()

    def _handler(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        if state_change != ServiceStateChange.Added:
            return
        if name.lower().startswith("winet-webserver"):
            seen_names.add(name)

    browser = AsyncServiceBrowser(zc, ["_http._tcp.local."], handlers=[_handler])
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()

    results: list[WinetDongle] = []
    seen_hosts: set[str] = set()
    for name in sorted(seen_names):
        info = AsyncServiceInfo("_http._tcp.local.", name)
        try:
            if not await info.async_request(zc, 2500):
                continue
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("could not resolve %s via mDNS: %s", name, err)
            continue
        addrs = info.ip_addresses_by_version(IPVersion.V4Only)
        if not addrs:
            continue
        host = str(addrs[0])
        # A dongle that answers on multiple interfaces can advertise the same host
        # more than once. Deduplicate — first hit wins, later ones are noise.
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        props = info.decoded_properties or {}
        serial, model = _parse_winet_properties(props)
        results.append(WinetDongle(host=host, serial=serial, model=model, mdns_name=info.server))
    return results


async def async_read_modbus_identity(host: str) -> tuple[str | None, str | None]:
    """Best-effort read of ``(model_name, serial)`` from a Sungrow inverter over Modbus.

    Opens a short-lived :class:`SungrowModbusClient` against ``host`` and reads the
    full realtime input-register set once. Pulls the ``inverter_serial`` string (10
    ASCII registers at wire 4989) and the ``device_type_code`` u16 at wire 4999 out
    of the result, mapping the type code to a human-readable model name via the
    shared ``resolve_enum_value`` table used by the sensor pipeline.

    Returns ``(None, None)`` on any transport / decode failure, so the wizard can
    fall through to the manual-details form instead of aborting. Either component
    may be ``None`` independently when the register wasn't populated by the firmware
    (very old dongles), which is the "partial success" case the wizard surfaces.
    """
    # Import late so this helper module stays cheap to import on every flow step —
    # the Modbus client pulls in ``pymodbus`` which is heavy.
    from ..measure_points import resolve_enum_value
    from ..modbus import SungrowModbusClient, SungrowModbusError

    client = SungrowModbusClient(host)
    try:
        try:
            data = await client.async_read_realtime()
        except SungrowModbusError as err:
            _LOGGER.debug("modbus identity read failed for %s: %s", host, err)
            return None, None
        serial_raw = data.get("inverter_serial", {}).get("value")
        serial = str(serial_raw).strip() if serial_raw else None
        type_code_raw = data.get("device_type_code", {}).get("value")
        model: str | None = None
        if type_code_raw is not None:
            try:
                model = resolve_enum_value("device_type_code", int(type_code_raw))
            except (ValueError, TypeError):
                model = None
        return model or None, serial or None
    finally:
        client.close()
