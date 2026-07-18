"""Unit tests for the #220 Modbus holding-control spike."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sungrow.modbus import SungrowModbusClient, SungrowModbusError
from custom_components.sungrow.modbus_control_probe import (
    WRITE_OK_ENV,
    HoldingProbeResult,
    classify_holding_probe_results,
    format_probe_summary,
    probe_holding_points,
    writes_allowed,
)
from custom_components.sungrow.modbus_registers import (
    HOLDING_WRITE_DENYLIST_WIRE,
    SG_RS_ACTIVE_POWER_LIMIT_RATIO,
    SG_RS_HOLDING_PROBE_POINTS,
    HoldingProbePoint,
)


def test_write_denylist_includes_start_stop_and_ems():
    """Spike must never target start/stop or hybrid EMS wires."""
    assert 5005 in HOLDING_WRITE_DENYLIST_WIRE
    assert 13049 in HOLDING_WRITE_DENYLIST_WIRE


def test_classify_supported_when_noop_write_ok():
    results = [
        HoldingProbeResult("ratio", 5007, "read", True, 1000, "raw=1000"),
        HoldingProbeResult("ratio", 5007, "write_noop", True, 1000, "noop ok"),
    ]
    assert classify_holding_probe_results(results) == "supported"


def test_classify_read_only_when_write_gated_off():
    results = [
        HoldingProbeResult("ratio", 5007, "read", True, 1000, "raw=1000"),
        HoldingProbeResult("ratio", 5007, "write_denied", False, 1000, "gated"),
    ]
    assert classify_holding_probe_results(results) == "read_only"


def test_classify_unsupported_when_all_reads_fail_non_connect():
    results = [
        HoldingProbeResult("a", 1, "read", False, None, "exception_code=2"),
        HoldingProbeResult("b", 2, "read", False, None, "exception_code=2"),
    ]
    assert classify_holding_probe_results(results) == "unsupported"


def test_classify_inconclusive_on_connect_failures():
    results = [
        HoldingProbeResult("a", 1, "read", False, None, "Could not connect to host"),
    ]
    assert classify_holding_probe_results(results) == "inconclusive"


def test_writes_allowed_requires_exact_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(WRITE_OK_ENV, raising=False)
    assert writes_allowed() is False
    monkeypatch.setenv(WRITE_OK_ENV, "1")
    assert writes_allowed() is True
    monkeypatch.setenv(WRITE_OK_ENV, "yes")
    assert writes_allowed() is False


async def test_probe_read_only_by_default():
    """Without write gate, readable write-candidates are recorded as write_denied."""
    client = MagicMock()
    client.async_read_holding = AsyncMock(return_value=[1000])
    client.async_write_holding = AsyncMock()
    point = HoldingProbePoint(
        name="ratio",
        wire_address=5007,
        description="test",
        family="sg_rs",
        write_candidate=True,
    )
    results = await probe_holding_points(client, (point,), allow_write=False)
    assert results[0].kind == "read" and results[0].ok
    assert results[1].kind == "write_denied"
    client.async_write_holding.assert_not_awaited()


async def test_probe_noop_write_when_allowed():
    client = MagicMock()
    client.async_read_holding = AsyncMock(side_effect=[[990], [990]])
    client.async_write_holding = AsyncMock()
    point = HoldingProbePoint(
        name="ratio",
        wire_address=5007,
        description="test",
        family="sg_rs",
        write_candidate=True,
    )
    results = await probe_holding_points(client, (point,), allow_write=True)
    assert any(r.kind == "write_noop" and r.ok for r in results)
    client.async_write_holding.assert_awaited_once_with(5007, 990)


async def test_probe_refuses_denylisted_write_candidate():
    """Even if marked write_candidate, denylist blocks the write."""
    client = MagicMock()
    client.async_read_holding = AsyncMock(return_value=[0])
    client.async_write_holding = AsyncMock()
    point = HoldingProbePoint(
        name="ems",
        wire_address=13049,
        description="denied",
        family="probe_negative",
        write_candidate=True,
    )
    results = await probe_holding_points(client, (point,), allow_write=True)
    assert any(r.kind == "write_denied" and "DENYLIST" in r.detail for r in results)
    client.async_write_holding.assert_not_awaited()


async def test_probe_records_read_failure():
    client = MagicMock()
    client.async_read_holding = AsyncMock(side_effect=SungrowModbusError("exception_code=2"))
    point = SG_RS_ACTIVE_POWER_LIMIT_RATIO
    results = await probe_holding_points(client, (point,), allow_write=False)
    assert len(results) == 1
    assert results[0].ok is False


def test_format_probe_summary_shape():
    results = [HoldingProbeResult("ratio", 5007, "read", True, 1000, "raw=1000")]
    summary = format_probe_summary(results, "read_only")
    assert summary["classification"] == "read_only"
    assert summary["results"][0]["wire_address"] == 5007


async def test_client_read_holding_uses_fc3_path():
    """async_read_holding calls read_holding_registers under the reconnect wrapper."""
    inner = MagicMock()
    inner.connected = True
    ok = MagicMock()
    ok.isError.return_value = False
    ok.registers = [1000]
    inner.read_holding_registers = AsyncMock(return_value=ok)
    inner.close = MagicMock()
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", return_value=inner):
        client = SungrowModbusClient("10.0.0.1")
        regs = await client.async_read_holding(5007, 1)
    assert regs == [1000]
    inner.read_holding_registers.assert_awaited_once()


async def test_client_write_holding_rejects_out_of_range():
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient"):
        client = SungrowModbusClient("10.0.0.1")
        with pytest.raises(SungrowModbusError, match="U16"):
            await client.async_write_holding(5007, 0x10000)


async def test_client_write_holding_calls_write_register():
    inner = MagicMock()
    inner.connected = True
    ok = MagicMock()
    ok.isError.return_value = False
    inner.write_register = AsyncMock(return_value=ok)
    inner.close = MagicMock()
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", return_value=inner):
        client = SungrowModbusClient("10.0.0.1")
        await client.async_write_holding(5007, 1000)
    inner.write_register.assert_awaited_once()
    args, kwargs = inner.write_register.await_args
    assert args[0] == 5007
    assert args[1] == 1000


def test_default_probe_points_cover_sg_and_negative():
    names = {p.name for p in SG_RS_HOLDING_PROBE_POINTS}
    assert "active_power_limit_ratio" in names
    assert "energy_management_mode" in names
