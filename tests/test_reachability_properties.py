"""Property-based tests for async_test_modbus_host (#216).

Feature: transport-mode-selector
"""

from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.sungrow.helpers import async_test_modbus_host

# ---------------------------------------------------------------------------
# Property 5: Reachability test uses correct port and timeout
# Validates: Requirements 12.1, 12.2
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(host=st.text(min_size=1, max_size=253))
@pytest.mark.asyncio
async def test_reachability_uses_correct_port_and_timeout(host: str):
    """Property 5: async_test_modbus_host calls open_connection with port=502, timeout=5."""
    mock_writer = AsyncMock()
    mock_writer.close = lambda: None
    mock_writer.wait_closed = AsyncMock()

    captured: dict[str, object] = {}

    async def _spy_wait_for(coro, *, timeout):
        # Actually consume the coroutine returned by open_connection so it
        # doesn't leak as an unawaited coroutine (RuntimeWarning noise).
        captured["timeout"] = timeout
        return await coro

    with (
        patch(
            "custom_components.sungrow.helpers.asyncio.open_connection",
            new_callable=AsyncMock,
            return_value=(AsyncMock(), mock_writer),
        ) as mock_conn,
        patch(
            "custom_components.sungrow.helpers.asyncio.wait_for",
            side_effect=_spy_wait_for,
        ) as mock_wait,
    ):
        await async_test_modbus_host(host)

    mock_wait.assert_called_once()
    assert captured["timeout"] == 5.0
    # open_connection was called with (host, port=502) via wait_for's spy.
    mock_conn.assert_awaited_once()
    conn_args, _ = mock_conn.call_args
    assert conn_args == (host, 502)


# ---------------------------------------------------------------------------
# Property 6: Reachability test returns bool without exceptions
# Validates: Requirements 12.3, 12.4
# ---------------------------------------------------------------------------


_EXCEPTIONS = [
    TimeoutError(),
    OSError("Connection refused"),
    ConnectionRefusedError("refused"),
    OSError("Name or service not known"),
    RuntimeError("unexpected"),
    ValueError("bad value"),
]


@settings(max_examples=100)
@given(
    host=st.text(min_size=1, max_size=253),
    exc_index=st.integers(min_value=0, max_value=len(_EXCEPTIONS) - 1),
    should_succeed=st.booleans(),
)
@pytest.mark.asyncio
async def test_reachability_returns_bool_no_exceptions(host: str, exc_index: int, should_succeed: bool):
    """Property 6: async_test_modbus_host always returns bool, never raises."""
    mock_writer = AsyncMock()
    mock_writer.close = lambda: None
    mock_writer.wait_closed = AsyncMock()

    async def _fake_open(*args, **kwargs):
        if should_succeed:
            return (AsyncMock(), mock_writer)
        raise _EXCEPTIONS[exc_index]

    with patch("custom_components.sungrow.helpers.asyncio.open_connection", side_effect=_fake_open):
        result = await async_test_modbus_host(host)

    assert isinstance(result, bool)
    if should_succeed:
        assert result is True
    else:
        assert result is False
