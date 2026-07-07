"""Tests for the local Modbus transport (#159)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sungrow.modbus import SungrowModbusClient, SungrowModbusError, merge_realtime
from custom_components.sungrow.modbus_registers import (
    DAILY_YIELD_DIAG_CANDIDATE_ADDRESSES,
    DAILY_YIELD_DIAG_COUNT,
    DAILY_YIELD_DIAG_START,
    SG_RS_INPUT_POINTS,
    ModbusPoint,
    block_bounds,
    daily_yield_diagnostic_dump,
    decode_registers,
)

# ---------------------------------------------------------------------------
# Register decoding (pure functions)
# ---------------------------------------------------------------------------


def test_decode_types_and_scales():
    """u16/s16/u32/s32 decode with the right sign, word order (low first) and scale."""
    points = (
        ModbusPoint(0, "u16_scaled", "u16", 0.1, "Hz"),
        ModbusPoint(1, "s16_neg", "s16", 0.1, "°C"),
        ModbusPoint(2, "u32_low_first", "u32", 1, "Wh"),
        ModbusPoint(4, "s32_neg", "s32", 1, "W"),
    )
    # u16=499 -> 49.9 ; s16=65486 (=-50) -> -5.0 ; u32=[300,1] -> 300+65536 ; s32=[0,0xFFFF]=-65536
    registers = [499, 65486, 300, 1, 0, 0xFFFF]
    out = decode_registers(points, 0, registers)
    assert out["u16_scaled"]["value"] == 49.9
    assert out["s16_neg"]["value"] == -5.0
    assert out["u32_low_first"]["value"] == 300 + 65536
    assert out["s32_neg"]["value"] == -65536
    # Shape matches the cloud transport + carries the source marker.
    assert out["u16_scaled"] == {"code": "u16_scaled", "value": 49.9, "unit": "Hz", "source": "modbus"}


def test_decode_skips_points_outside_the_block():
    """A point whose registers fall outside the read block is skipped, not mis-decoded."""
    points = (ModbusPoint(10, "in", "u16", 1, None), ModbusPoint(99, "out", "u16", 1, None))
    out = decode_registers(points, 10, [42])
    assert "in" in out and out["in"]["value"] == 42
    assert "out" not in out


def test_block_bounds_covers_all_points_in_one_read():
    """block_bounds spans from the lowest address to past the highest (incl. 32-bit width)."""
    start, count = block_bounds(SG_RS_INPUT_POINTS)
    assert start == 4999  # device_type_code
    # Highest point is grid_frequency at 5035 (1 register) -> end 5036.
    assert start + count == 5036
    assert count == 37


def test_sg_rs_map_decodes_a_realistic_frame():
    """A realistic SG-RS input-register frame decodes to sane, correctly-scaled values."""
    start, count = block_bounds(SG_RS_INPUT_POINTS)
    regs = [0] * count

    def put(addr, *values):
        for i, v in enumerate(values):
            regs[addr - start + i] = v

    put(4999, 9732)  # device type
    put(5002, 389)  # daily yield 38.9 kWh
    put(5003, 6305, 0)  # total yield 6305 kWh (u32 low-first)
    put(5007, 472)  # internal temp 47.2 C
    put(5016, 300, 0)  # total DC power 300 W
    put(5018, 2404)  # phase A voltage 240.4 V
    put(5030, 259, 0)  # total active power 259 W
    put(5035, 499)  # grid frequency 49.9 Hz

    out = decode_registers(SG_RS_INPUT_POINTS, start, regs)
    assert out["daily_yield"]["value"] == 38.9
    assert out["total_yield"]["value"] == 6305
    assert out["internal_temperature"]["value"] == 47.2
    assert out["total_dc_power"]["value"] == 300
    assert out["phase_a_voltage"]["value"] == 240.4
    assert out["total_active_power"]["value"] == 259
    assert out["grid_frequency"]["value"] == 49.9  # regression: scale is ×0.1, not ×0.01


# ---------------------------------------------------------------------------
# SungrowModbusClient (pymodbus mocked)
# ---------------------------------------------------------------------------


def _mock_client_cls(registers, *, connected=True, connect_ok=True, is_error=False):
    """Patch AsyncModbusTcpClient with a stub whose read returns ``registers``."""
    inner = MagicMock()
    inner.connected = connected
    inner.connect = AsyncMock(return_value=connect_ok)
    result = MagicMock()
    result.isError.return_value = is_error
    result.registers = registers
    inner.read_input_registers = AsyncMock(return_value=result)
    inner.close = MagicMock()
    cls = MagicMock(return_value=inner)
    return cls, inner


async def test_read_realtime_returns_decoded_points():
    """A successful read yields the decoded SG-RS points tagged source=modbus."""
    start, count = block_bounds(SG_RS_INPUT_POINTS)
    regs = [0] * count
    regs[5035 - start] = 499  # grid frequency
    cls, inner = _mock_client_cls(regs)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", unit=1)
        data = await client.async_read_realtime()
    assert data["grid_frequency"]["value"] == 49.9
    assert data["grid_frequency"]["source"] == "modbus"
    inner.read_input_registers.assert_awaited_once()


async def test_read_connects_when_not_connected():
    """The client connects before reading if the socket isn't up yet."""
    cls, inner = _mock_client_cls([0] * block_bounds(SG_RS_INPUT_POINTS)[1], connected=False)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        await SungrowModbusClient("10.0.0.1").async_read_realtime()
    inner.connect.assert_awaited_once()


async def test_connect_failure_raises():
    """A failed connect raises SungrowModbusError."""
    cls, _ = _mock_client_cls([], connected=False, connect_ok=False)
    with (
        patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls),
        pytest.raises(SungrowModbusError, match="Could not connect"),
    ):
        await SungrowModbusClient("10.0.0.1").async_read_realtime()


async def test_read_error_raises_and_drops_connection():
    """A Modbus error result raises and closes the connection so the next read reconnects."""
    cls, inner = _mock_client_cls([0] * 37, is_error=True)
    with (
        patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls),
        pytest.raises(SungrowModbusError, match="failed"),
    ):
        await SungrowModbusClient("10.0.0.1").async_read_realtime()
    inner.close.assert_called_once()


async def test_unknown_model_raises():
    """An unmapped model raises rather than silently returning nothing."""
    cls, _ = _mock_client_cls([])
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", model="sh_hybrid_not_yet_mapped")
        with pytest.raises(SungrowModbusError, match="register map"):
            await client.async_read_realtime()


async def test_read_passes_configured_unit_as_device_id():
    """The configured unit id is forwarded to pymodbus as device_id."""
    cls, inner = _mock_client_cls([0] * block_bounds(SG_RS_INPUT_POINTS)[1])
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        await SungrowModbusClient("10.0.0.1", unit=3).async_read_realtime()
    assert inner.read_input_registers.await_args.kwargs["device_id"] == 3


# ---------------------------------------------------------------------------
# merge_realtime (cloud + modbus, Modbus preferred, provenance)
# ---------------------------------------------------------------------------


def test_merge_prefers_modbus_and_tags_provenance():
    """Shared codes take the Modbus value/unit; every point carries its source."""
    cloud = {
        "total_active_power": {"code": "total_active_power", "value": "250", "unit": "W", "id": "5031", "name": "AC"},
        "daily_yield": {"code": "daily_yield", "value": "38.0", "unit": "kWh"},
    }
    modbus = {"total_active_power": {"code": "total_active_power", "value": 256, "unit": "W", "source": "modbus"}}
    merged = merge_realtime(cloud, modbus)
    # Modbus value wins for the shared code, and cloud metadata (id/name) is kept.
    assert merged["total_active_power"]["value"] == 256
    assert merged["total_active_power"]["source"] == "modbus"
    assert merged["total_active_power"]["id"] == "5031"
    # Cloud-only point is retained and tagged cloud.
    assert merged["daily_yield"]["value"] == "38.0"
    assert merged["daily_yield"]["source"] == "cloud"


def test_merge_adds_modbus_only_points():
    """A point only Modbus exposes is added to the merged result."""
    merged = merge_realtime({}, {"grid_frequency": {"code": "grid_frequency", "value": 49.9, "source": "modbus"}})
    assert merged["grid_frequency"]["value"] == 49.9
    assert merged["grid_frequency"]["source"] == "modbus"


def test_daily_yield_diagnostic_dump_lists_every_candidate_address_and_scale():
    """The dump enumerates every (candidate address, candidate scale) so a daytime
    re-capture can pick the right mapping without guessing (#223).
    """
    registers = [0] * DAILY_YIELD_DIAG_COUNT
    registers[5000 - DAILY_YIELD_DIAG_START] = 10  # address 5000
    registers[5002 - DAILY_YIELD_DIAG_START] = 640  # address 5002 (current mapping)
    registers[5003 - DAILY_YIELD_DIAG_START] = 6330  # address 5003
    dump = daily_yield_diagnostic_dump(registers, DAILY_YIELD_DIAG_START)
    assert dump["start"] == DAILY_YIELD_DIAG_START
    assert dump["raw"]["5002"] == 640
    assert dump["raw"]["5003"] == 6330
    # The current mapping is echoed for one-glance comparison with the live entity.
    assert dump["current_mapping"] == {"address": 5002, "raw": 640, "scale": 0.1, "unit": "kWh"}
    # Every (address, scale) candidate the diagnostic tracks is present.
    candidate_pairs = {(c["address"], c["scale"]) for c in dump["candidates"]}
    for address in DAILY_YIELD_DIAG_CANDIDATE_ADDRESSES:
        for scale, _ in ((0.1, "kWh"), (0.01, "kWh"), (1.0, "Wh"), (10.0, "—")):
            assert (address, scale) in candidate_pairs


def test_daily_yield_diagnostic_dump_surfaces_current_mapping_match():
    """When the current mapping matches the live value, the candidate list shows it (#223)."""
    registers = [0] * DAILY_YIELD_DIAG_COUNT
    registers[5002 - DAILY_YIELD_DIAG_START] = 640  # 640 * 0.1 = 64.0 kWh (current mapping)
    dump = daily_yield_diagnostic_dump(registers, DAILY_YIELD_DIAG_START)
    # The 0.1-scale candidate at address 5002 is exactly the value the live entity would show.
    matching = [c for c in dump["candidates"] if c["address"] == 5002 and c["scale"] == 0.1]
    assert matching and matching[0]["value"] == 64.0


def test_daily_yield_diagnostic_dump_handles_partial_block():
    """A truncated block is accepted: only the present addresses show up (#223)."""
    registers = [0, 0, 0]  # 4999..5001 only
    registers[5001 - DAILY_YIELD_DIAG_START] = 7
    dump = daily_yield_diagnostic_dump(registers, DAILY_YIELD_DIAG_START)
    assert dump["raw"]["5001"] == 7
    # No candidates past the block end.
    assert not any(c["address"] >= 5002 for c in dump["candidates"])


async def test_modbus_client_diagnostic_dump_reads_diag_window():
    """The Modbus client surfaces the diagnostic by reading the dedicated #223 window."""
    registers = [0] * DAILY_YIELD_DIAG_COUNT
    registers[5002 - DAILY_YIELD_DIAG_START] = 640
    cls, inner = _mock_client_cls(registers, connected=True)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1")
        diag = await client.async_read_daily_yield_diagnostic()
    # The window the diagnostic cares about was read.
    inner.read_input_registers.assert_awaited_once()
    assert inner.read_input_registers.await_args.args[0] == DAILY_YIELD_DIAG_START
    assert inner.read_input_registers.await_args.kwargs["count"] == DAILY_YIELD_DIAG_COUNT
    # The current-mapping entry ties the read back to the existing decode.
    assert diag["current_mapping"] == {"address": 5002, "raw": 640, "scale": 0.1, "unit": "kWh"}


async def test_modbus_client_diagnostic_dump_raises_on_read_error():
    """A Modbus read error surfaces as SungrowModbusError so the caller can keep the
    previous diagnostic instead of clearing evidence the user is collecting (#223).
    """
    cls, inner = _mock_client_cls([0] * DAILY_YIELD_DIAG_COUNT, is_error=True)
    with (
        patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls),
        pytest.raises(SungrowModbusError, match="failed"),
    ):
        await SungrowModbusClient("10.0.0.1").async_read_daily_yield_diagnostic()
    inner.close.assert_called_once()
