"""Shared helper utilities for the Sungrow iSolarCloud integration."""

import asyncio
from collections.abc import Mapping
from typing import Any

from .const import CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT


async def async_test_modbus_host(host: str, port: int = DEFAULT_MODBUS_PORT, timeout: float = 5.0) -> bool:
    """Test TCP reachability of a Modbus host.

    Attempts a TCP connection to host:port with the given timeout.
    Returns True if connection succeeds (socket is closed immediately).
    Returns False on any failure (timeout, refused, DNS error) without raising.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        return False
    return True


def resolve_modbus_port(options: Mapping[str, Any], data: Mapping[str, Any]) -> int:
    """Return the Modbus TCP port for an entry: options, then data, then 502 (#485).

    The config flow stores the port in entry data next to the host (manual wizard,
    reconfigure, import). The options flow only writes an override when the user picks
    a port that differs from data, so options win when present. Entries created
    before #485 carry neither key and keep using the standard port.
    """
    for source in (options, data):
        value = source.get(CONF_MODBUS_PORT)
        if value is not None and value != "":
            return int(value)
    return DEFAULT_MODBUS_PORT
