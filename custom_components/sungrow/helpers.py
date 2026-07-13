"""Shared helper utilities for the Sungrow iSolarCloud integration."""

import asyncio


async def async_test_modbus_host(host: str, port: int = 502, timeout: float = 5.0) -> bool:
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
