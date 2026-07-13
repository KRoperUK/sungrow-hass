"""Tests for the local Modbus transport (#159)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sungrow.modbus import SungrowModbusClient, SungrowModbusError
from custom_components.sungrow.modbus_registers import (
    DAILY_YIELD_DIAG_CANDIDATE_ADDRESSES,
    DAILY_YIELD_DIAG_COUNT,
    DAILY_YIELD_DIAG_START,
    DEVICE_TYPE_CODE_TO_FAMILY,
    SG_RS_INPUT_POINTS,
    SH_RT_INPUT_POINTS,
    ModbusPoint,
    block_bounds,
    block_partitions,
    daily_yield_diagnostic_dump,
    decode_registers,
    family_for_device_type_code,
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


def test_decode_omits_nan_sentinel_values():
    """Points marked with nan_value are omitted when the raw register matches."""
    points = (
        ModbusPoint(0, "present", "u16", 0.1, "V"),
        ModbusPoint(1, "absent", "u16", 0.1, "V", nan_value=0xFFFF),
    )
    out = decode_registers(points, 0, [2404, 0xFFFF])
    assert "present" in out
    assert "absent" not in out


def test_block_bounds_covers_all_points_in_one_read():
    """block_bounds spans from the lowest address to past the highest (incl. 32-bit width)."""
    points = (
        ModbusPoint(10, "first", "u16", 1),
        ModbusPoint(20, "wide", "u32", 1),
        ModbusPoint(40, "last", "u16", 1),
    )
    start, count = block_bounds(points)
    assert start == 10
    assert start + count == 41


def test_block_partitions_splits_wide_gaps():
    """Wide address gaps are split into separate reads."""
    points = (
        ModbusPoint(10, "low", "u16", 1),
        ModbusPoint(300, "high", "u16", 1),
        ModbusPoint(310, "higher", "u16", 1),
    )
    blocks = block_partitions(points, max_gap=256)
    assert blocks == [(10, 1), (300, 11)]


def test_block_partitions_keeps_nearby_points_together():
    """Nearby points are kept in a single block."""
    points = (
        ModbusPoint(10, "a", "u16", 1),
        ModbusPoint(12, "b", "u16", 1),
        ModbusPoint(15, "c", "u32", 1),
    )
    blocks = block_partitions(points, max_gap=256)
    assert blocks == [(10, 7)]


def test_sh_rt_map_partitions_around_large_gaps():
    """The SH-RT map is split around gaps > 256 registers."""
    blocks = block_partitions(SH_RT_INPUT_POINTS)
    assert len(blocks) == 3
    assert blocks[0][0] == 4999
    assert blocks[1][0] == 5600
    assert blocks[2][0] == 12999


def test_sg_rs_low_block_decodes_a_realistic_frame():
    """A realistic SG-RS low-register frame decodes to sane, correctly-scaled values."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
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

    out = decode_registers(low_points, start, regs)
    assert out["daily_yield"]["value"] == 38.9
    assert out["total_yield"]["value"] == 6305
    assert out["internal_temperature"]["value"] == 47.2
    assert out["total_dc_power"]["value"] == 300
    assert out["phase_a_voltage"]["value"] == 240.4
    assert out["total_active_power"]["value"] == 259
    assert out["grid_frequency"]["value"] == 49.9  # regression: scale is ×0.1, not ×0.01


def test_family_for_device_type_code_maps_known_codes():
    """Known device-type codes resolve to a register-map family."""
    for code, family in DEVICE_TYPE_CODE_TO_FAMILY.items():
        assert family_for_device_type_code(code) == family


def test_family_for_device_type_code_returns_none_for_unknown():
    """Unknown codes return None so callers can fall back."""
    assert family_for_device_type_code(99999) is None
    assert family_for_device_type_code(None) is None


# ---------------------------------------------------------------------------
# SungrowModbusClient (pymodbus mocked)
# ---------------------------------------------------------------------------


def _mock_client_cls(registers, *, connected=True, connect_ok=True, is_error=False):
    """Patch AsyncModbusTcpClient with a stub whose read returns ``registers``.

    The mock always returns the same register list regardless of the requested
    address/count, which is good enough for the tests that only look at one
    specific offset.
    """
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


def _skip_family_detect(client: SungrowModbusClient) -> None:
    """Mark family detection done so tests can focus on a single read path."""
    client._family_detected = True  # noqa: SLF001


async def test_read_realtime_returns_decoded_points():
    """A successful read yields the decoded SG-RS points tagged source=modbus."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    regs = [0] * count
    regs[5035 - start] = 499  # grid frequency
    cls, inner = _mock_client_cls(regs)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", unit=1)
        _skip_family_detect(client)
        # Restrict to the low block so the test only exercises one read.
        client.model = "_test_sg_rs_low"
        with patch.dict("custom_components.sungrow.modbus.REGISTER_MAPS", {"_test_sg_rs_low": low_points}, clear=False):
            data = await client.async_read_realtime()
    assert data["grid_frequency"]["value"] == 49.9
    assert data["grid_frequency"]["source"] == "modbus"
    inner.read_input_registers.assert_awaited_once()


async def test_read_connects_when_not_connected():
    """The client connects before reading if the socket isn't up yet."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    cls, inner = _mock_client_cls([0] * count, connected=False)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1")
        _skip_family_detect(client)
        client.model = "_test_sg_rs_low"
        with patch.dict("custom_components.sungrow.modbus.REGISTER_MAPS", {"_test_sg_rs_low": low_points}, clear=False):
            await client.async_read_realtime()
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
        client = SungrowModbusClient("10.0.0.1")
        _skip_family_detect(client)
        client.model = "_test_error"
        with patch.dict(
            "custom_components.sungrow.modbus.REGISTER_MAPS",
            {"_test_error": (ModbusPoint(0, "x", "u16", 1),)},
            clear=False,
        ):
            await client.async_read_realtime()
    inner.close.assert_called_once()


async def test_unknown_model_raises():
    """An unmapped model raises rather than silently returning nothing."""
    cls, _ = _mock_client_cls([])
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", model="sh_hybrid_not_yet_mapped")
        _skip_family_detect(client)
        with pytest.raises(SungrowModbusError, match="register map"):
            await client.async_read_realtime()


async def test_read_passes_configured_unit_as_device_id():
    """The configured unit id is forwarded to pymodbus as device_id."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    cls, inner = _mock_client_cls([0] * count)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", unit=3)
        _skip_family_detect(client)
        client.model = "_test_sg_rs_low"
        with patch.dict("custom_components.sungrow.modbus.REGISTER_MAPS", {"_test_sg_rs_low": low_points}, clear=False):
            await client.async_read_realtime()
    assert inner.read_input_registers.await_args.kwargs["device_id"] == 3


async def test_family_auto_detection_reads_device_type_code_and_switches_map():
    """On first read the client detects family from register 5000 and switches maps."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    regs = [0] * count
    regs[4999 - start] = 9732  # SG-RS device-type code
    regs[5035 - start] = 499  # grid frequency
    cls, inner = _mock_client_cls(regs)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1")
        # Restrict to low block to keep the test focused on one read after detection.
        with patch.dict("custom_components.sungrow.modbus.REGISTER_MAPS", {"sg_rs": low_points}, clear=False):
            data = await client.async_read_realtime()
    assert client.model == "sg_rs"
    assert data["grid_frequency"]["value"] == 49.9
    # First call is family detection, second call is the block read.
    assert inner.read_input_registers.await_count == 2


async def test_family_auto_detection_falls_back_to_configured_model_for_unknown_code():
    """An unknown device-type code keeps the configured model."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    regs = [0] * count
    regs[4999 - start] = 12345  # Unknown device-type code
    regs[5035 - start] = 499
    cls, inner = _mock_client_cls(regs)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", model="sg_rs")
        with patch.dict("custom_components.sungrow.modbus.REGISTER_MAPS", {"sg_rs": low_points}, clear=False):
            await client.async_read_realtime()
    assert client.model == "sg_rs"
    assert inner.read_input_registers.await_count == 2


# ---------------------------------------------------------------------------
# #223 diagnostic dump
# ---------------------------------------------------------------------------


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
