"""Tests for the local Modbus transport (#159)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sungrow.modbus import SungrowModbusClient, SungrowModbusError
from custom_components.sungrow.modbus_registers import (
    DAILY_YIELD_DIAG_CANDIDATE_ADDRESSES,
    DAILY_YIELD_DIAG_COUNT,
    DAILY_YIELD_DIAG_START,
    DEVICE_TYPE_CODE_TO_FAMILY,
    REGISTER_MAPS,
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


# ---------------------------------------------------------------------------
# String data_type support (#323)
# ---------------------------------------------------------------------------
# Sungrow packs ASCII fields (serial number, firmware version) in consecutive
# registers with two bytes per register, high byte first. The decoder must
# reconstruct the string, strip trailing NUL padding, and treat a fully empty
# field as "not populated" so unsupported firmwares don't produce empty sensors.


def test_string_point_register_count_uses_length():
    """A string ModbusPoint spans ``length`` registers, not the numeric-type fallback."""
    point = ModbusPoint(4989, "inverter_serial", "string", 1, None, length=10)
    assert point.register_count == 10


def test_string_point_rejects_zero_length():
    """A string with no declared length is a configuration error, not silent success."""
    point = ModbusPoint(4989, "bad", "string", 1, None)  # length defaults to 0
    with pytest.raises(ValueError, match="length"):
        _ = point.register_count


def test_decode_string_reads_big_endian_ascii():
    """Two ASCII bytes per register, high byte first — the mkaiser/Sungrow convention."""
    point = ModbusPoint(0, "inverter_serial", "string", 1, None, length=5)
    # 'A'=0x41, 'B'=0x42, ...
    registers = [0x4142, 0x4344, 0x4546, 0x4748, 0x494A]
    out = decode_registers((point,), 0, registers)
    assert out["inverter_serial"]["value"] == "ABCDEFGHIJ"
    # Strings are unitless and still tagged source=modbus.
    assert out["inverter_serial"]["unit"] is None
    assert out["inverter_serial"]["source"] == "modbus"


def test_decode_string_strips_trailing_nul_padding():
    """Sungrow pads short serials with NUL — never leak them into HA."""
    point = ModbusPoint(0, "inverter_serial", "string", 1, None, length=4)
    # "A12" + trailing NULs across two registers.
    registers = [0x4131, 0x3200, 0x0000, 0x0000]  # "A", "1", "2", NUL...
    out = decode_registers((point,), 0, registers)
    assert out["inverter_serial"]["value"] == "A12"


def test_decode_string_omitted_when_all_nul():
    """A fully NUL / empty field means the firmware doesn't expose the point."""
    point = ModbusPoint(0, "battery_firmware_version", "string", 1, None, length=4)
    registers = [0x0000] * 4
    out = decode_registers((point,), 0, registers)
    assert "battery_firmware_version" not in out


def test_decode_string_strips_non_printable_control_bytes():
    """Some firmwares leak zero-padding mid-string; those bytes get dropped cleanly."""
    point = ModbusPoint(0, "inverter_firmware_version", "string", 1, None, length=3)
    # "V1" + NUL + more real chars + NUL, imitating a corrupted mid-string zero.
    registers = [0x5631, 0x0056, 0x3200]  # 'V','1',NUL,'V','2',NUL
    out = decode_registers((point,), 0, registers)
    # Interior NULs are stripped; the printable characters remain.
    assert out["inverter_firmware_version"]["value"] == "V1V2"


def test_decode_string_survives_undecodable_bytes():
    """Non-ASCII bytes are silently dropped (support snapshots must still round-trip)."""
    point = ModbusPoint(0, "inverter_serial", "string", 1, None, length=3)
    # 0xFF is not valid ASCII; the decoder should ignore it, not raise.
    registers = [0x4142, 0xFFFF, 0x4344]  # "AB", <invalid>, "CD"
    out = decode_registers((point,), 0, registers)
    assert out["inverter_serial"]["value"] == "ABCD"


def test_decode_string_and_numeric_share_a_block():
    """A string point and neighbouring numeric points decode together in one block."""
    points = (
        ModbusPoint(0, "inverter_serial", "string", 1, None, length=3),
        ModbusPoint(4, "device_type_code", "u16", 1, None),
    )
    registers = [0x5347, 0x2D33, 0x2E36, 0, 3355]  # "SG-3.6" + padding + 3355
    out = decode_registers(points, 0, registers)
    assert out["inverter_serial"]["value"] == "SG-3.6"
    assert out["device_type_code"]["value"] == 3355


def test_block_partitions_counts_string_length_for_cap():
    """A 15-register string is charged 15 registers against the 125-cap accounting."""
    points = (
        ModbusPoint(1000, "big_string", "string", 1, None, length=15),
        ModbusPoint(1020, "neighbour", "u16", 1),
    )
    # With a 10-register cap the string alone exceeds the cap — verify the
    # partitioner then breaks between the two points rather than merging.
    blocks = block_partitions(points, max_block_size=10)
    assert all(count <= 15 for _, count in blocks), blocks  # cap is clamped up to point size
    # Two distinct blocks because the neighbour doesn't fit in the same read.
    assert len(blocks) == 2
    # But it MUST fit in the standard 100-register cap.
    blocks_default = block_partitions(points)
    assert len(blocks_default) == 1
    assert blocks_default[0][1] == 21  # 15 (string) + 5 (gap) + 1 (u16)


def test_block_bounds_includes_full_string_span():
    """block_bounds reports the trailing register of a string point too."""
    points = (
        ModbusPoint(4989, "inverter_serial", "string", 1, None, length=10),
        ModbusPoint(5030, "total_active_power", "s32", 1, "W"),
    )
    start, count = block_bounds(points)
    assert start == 4989
    assert start + count == 5032  # s32 ends at 5030+2


# ---------------------------------------------------------------------------
# ARM + DSP subsystem software strings (#333)
# ---------------------------------------------------------------------------
# The register audit reconciled our SG-RS + SH-RT maps against mkaiser and
# TCzerny. Both projects expose 15-register string fields at wires 4953
# (ARM software) and 4968 (DSP software); we surface them as diagnostic
# sensors that fall through the empty-string skip when unpopulated.


@pytest.mark.parametrize("map_name", ["sg_rs", "sh_rt"])
def test_arm_and_dsp_strings_present_in_family_maps(map_name):
    """Both string points appear in the SG-RS and SH-RT input maps (#333)."""
    from custom_components.sungrow.modbus_registers import REGISTER_MAPS

    codes = {p.code: p for p in REGISTER_MAPS[map_name]}
    for code, address, length in (
        ("arm_software_version", 4953, 15),
        ("dsp_software_version", 4968, 15),
    ):
        point = codes.get(code)
        assert point is not None, f"{code} missing from {map_name}"
        assert point.data_type == "string"
        assert point.address == address
        assert point.length == length


def test_arm_and_dsp_strings_are_diagnostic():
    """LOCAL_IDENTITY_CODES gates the sensor layer's DIAGNOSTIC categorisation (#333)."""
    from custom_components.sungrow.modbus_registers import LOCAL_IDENTITY_CODES

    assert "arm_software_version" in LOCAL_IDENTITY_CODES
    assert "dsp_software_version" in LOCAL_IDENTITY_CODES


def test_arm_and_dsp_strings_decode_from_realistic_bytes():
    """A 15-register block round-trips into a printable version string."""
    from custom_components.sungrow.modbus_registers import SG_RS_INPUT_POINTS

    arm = next(p for p in SG_RS_INPUT_POINTS if p.code == "arm_software_version")
    # "SAPPHIRE-H_01011.95.12" — mkaiser doc example, packed high-byte first.
    text = "SAPPHIRE-H_01011.95.12"
    padded = text.ljust(arm.length * 2, "\x00")  # 30 bytes total for 15 regs
    registers = [(ord(padded[i]) << 8) | ord(padded[i + 1]) for i in range(0, arm.length * 2, 2)]
    out = decode_registers((arm,), arm.address, registers)
    assert out["arm_software_version"]["value"] == "SAPPHIRE-H_01011.95.12"
    assert out["arm_software_version"]["unit"] is None
    assert out["arm_software_version"]["source"] == "modbus"


def test_sg_rs_low_block_still_fits_after_arm_dsp_addition():
    """The SG-RS low block (4953..5035) stays comfortably under the 125-register cap (#333)."""
    from custom_components.sungrow.modbus_registers import SG_RS_INPUT_POINTS

    blocks = block_partitions(SG_RS_INPUT_POINTS)
    # A single low block covers everything from ARM software to the last SG-RS
    # numeric point. It must fit under 125 registers so pymodbus accepts it.
    assert len(blocks) == 1
    _, count = blocks[0]
    assert count <= 125


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


def test_sh_rt_map_partitions_stay_under_modbus_count_cap():
    """The SH-RT map partitions into reads small enough for a single Modbus request (#318).

    Before this fix, points 4999..5241 collapsed into a single 243-register block
    that pymodbus refuses to send (spec cap = 125). Every block must now fit and
    the map must still cover the full register range from the serial-number
    block up past the high hybrid registers.
    """
    blocks = block_partitions(SH_RT_INPUT_POINTS)
    # Every read must be within the Modbus function-4 protocol limit.
    assert blocks, "expected at least one block"
    assert all(count <= 125 for _, count in blocks), blocks
    # First block starts at or before the device-type register 4999 (the serial
    # at 4989 pulls the block start down when local identity strings are mapped).
    assert blocks[0][0] <= 4999
    # Full range still covered: last block must reach the high hybrid registers.
    # 13279 + 15 = 13294 for the battery firmware string, or 13047 for the last
    # numeric point on models that don't expose the firmware strings.
    assert max(start + count for start, count in blocks) >= 13046


def test_block_partitions_enforces_max_block_size():
    """A cluster larger than ``max_block_size`` is split into multiple reads."""
    # 200 contiguous u16 points would be one block by gap alone, but must be
    # split when ``max_block_size`` is enforced.
    points = tuple(ModbusPoint(1000 + i, f"p{i}", "u16", 1) for i in range(200))
    blocks = block_partitions(points, max_block_size=100)
    assert len(blocks) >= 2
    assert all(count <= 100 for _, count in blocks)
    # No point is dropped: registers [1000, 1200) are all covered.
    covered = {addr for start, count in blocks for addr in range(start, start + count)}
    assert covered == {1000 + i for i in range(200)}


def test_block_partitions_clamps_to_modbus_protocol_max():
    """``max_block_size`` cannot exceed the 125-register Modbus function-4 cap."""
    # Ask for a huge cap; the function must still keep every block <= 125.
    points = tuple(ModbusPoint(1000 + i, f"p{i}", "u16", 1) for i in range(300))
    blocks = block_partitions(points, max_block_size=10_000)
    assert all(count <= 125 for _, count in blocks), blocks


# ---------------------------------------------------------------------------
# Register-map safety (regression guard for #318)
# ---------------------------------------------------------------------------
# These tests are parametrized over every family in ``REGISTER_MAPS`` so a new
# map added later cannot silently reintroduce the 243-register block that
# pymodbus refuses to send. If someone extends SH-RT with points that widen the
# 4999..5241 cluster past 125 again, the guard here trips before it ever hits
# a user's WiNet-S.


@pytest.mark.parametrize("family", sorted(REGISTER_MAPS.keys()))
def test_every_register_map_partitions_within_modbus_cap(family):
    """Every family's map splits into reads that fit a single Modbus request (#318)."""
    points = REGISTER_MAPS[family]
    blocks = block_partitions(points)
    for start, count in blocks:
        assert 1 <= count <= 125, f"{family}: block {(start, count)} exceeds Modbus cap"


@pytest.mark.parametrize("family", sorted(REGISTER_MAPS.keys()))
def test_every_register_map_point_is_covered_by_a_block(family):
    """Partitioning never drops a point: each address range sits inside one block (#318)."""
    points = REGISTER_MAPS[family]
    blocks = block_partitions(points)
    for point in points:
        point_end = point.address + point.register_count
        covered = any(start <= point.address and point_end <= start + count for start, count in blocks)
        assert covered, f"{family}: point {point.code}@{point.address} not covered by any block"


def _capped_modbus_client_cls(*, cap: int = 125):
    """Fake pymodbus client that mirrors the real 125-register-per-request cap.

    Any read whose ``count`` exceeds ``cap`` is returned as an error response with
    the exact message shape pymodbus surfaces (``1 < count N < 125 !``), so the
    end-to-end test reproduces the failure users hit in #318 rather than silently
    passing when a map explodes.
    """
    inner = MagicMock()
    inner.connected = True
    inner.connect = AsyncMock(return_value=True)
    inner.close = MagicMock()
    inner.requested = []

    async def _read(address, count, device_id):
        inner.requested.append((address, count))
        result = MagicMock()
        if count > cap:
            result.isError.return_value = True
            result.__str__ = lambda _s: f"1 < count {count} < {cap} !"
        else:
            result.isError.return_value = False
            result.registers = [0] * count
        return result

    inner.read_input_registers = AsyncMock(side_effect=_read)
    cls = MagicMock(return_value=inner)
    return cls, inner


@pytest.mark.parametrize("family", sorted(REGISTER_MAPS.keys()))
async def test_read_realtime_never_asks_pymodbus_for_more_than_the_cap(family):
    """End-to-end: reading each family never triggers pymodbus's 125-register rejection (#318).

    This is the regression that would have caught the SH10RS crash before release:
    on the buggy code path the SH-RT/SH-RS map produced a 243-register read that
    a cap-enforcing fake pymodbus rejects with ``1 < count 243 < 125 !`` — exactly
    the error users reported.
    """
    cls, inner = _capped_modbus_client_cls(cap=125)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1", model=family)
        _skip_family_detect(client)
        # Should not raise: every block read must fit under 125 registers.
        await client.async_read_realtime()
    assert inner.requested, f"{family}: expected at least one read"
    assert all(count <= 125 for _, count in inner.requested), (
        f"{family}: pymodbus would reject reads {[c for _, c in inner.requested if c > 125]}"
    )


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
    # Spot-check SH hybrids from the mkaiser device-type map (#219).
    assert family_for_device_type_code(0x0E03) == "sh_rt"  # SH10RT
    assert family_for_device_type_code(0x0D0F) == "sh_rs"  # SH5.0RS
    # SG family — every documented SG-RS / SG-RT variant now resolves (#330).
    assert family_for_device_type_code(0x2603) == "sg_rs"  # SG3.0RS
    assert family_for_device_type_code(0x2604) == "sg_rs"  # SG3.6RS
    assert family_for_device_type_code(0x2609) == "sg_rs"  # SG10RS
    assert family_for_device_type_code(0x2430) == "sg_rt"  # SG5.0RT
    assert family_for_device_type_code(0x2433) == "sg_rt"  # SG10RT
    assert family_for_device_type_code(0x2437) == "sg_rt"  # SG20RT
    # MG hybrids fill out the small-hybrid family (#330).
    assert family_for_device_type_code(0x0D29) == "sh_rs"  # MG8RL
    assert family_for_device_type_code(0x0D2A) == "sh_rs"  # MG10RL


def test_family_for_device_type_code_returns_none_for_unknown():
    """Unknown codes return None so callers can fall back."""
    assert family_for_device_type_code(99999) is None
    assert family_for_device_type_code(None) is None


def test_register_maps_cover_all_model_families():
    """Every ModelFamily that has a map key is registered (#219)."""
    assert set(REGISTER_MAPS) >= {"sg_rs", "sg_rt", "sh_rs", "sh_rt"}
    assert REGISTER_MAPS["sg_rt"] is REGISTER_MAPS["sg_rs"]
    assert REGISTER_MAPS["sh_rs"] is REGISTER_MAPS["sh_rt"]


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
    """A failed connect raises SungrowModbusError after a recreate attempt."""
    cls, _ = _mock_client_cls([], connected=False, connect_ok=False)
    with (
        patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls),
        pytest.raises(SungrowModbusError, match="Could not connect"),
    ):
        await SungrowModbusClient("10.0.0.1").async_read_realtime()


async def test_connect_failure_recreates_and_succeeds():
    """When the first connect() fails, a fresh client is opened and the read continues."""
    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    regs = [0] * count
    regs[5035 - start] = 501

    dead = MagicMock()
    dead.connected = False
    dead.connect = AsyncMock(return_value=False)
    dead.close = MagicMock()

    healthy = MagicMock()
    healthy.connected = False
    healthy.connect = AsyncMock(return_value=True)
    healthy.close = MagicMock()
    ok = MagicMock()
    ok.isError.return_value = False
    ok.registers = regs
    healthy.read_input_registers = AsyncMock(return_value=ok)

    # __init__ builds dead; ensure_connected recreates → healthy.
    cls = MagicMock(side_effect=[dead, healthy])
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1")
        _skip_family_detect(client)
        client.model = "_test_connect_heal"
        with patch.dict(
            "custom_components.sungrow.modbus.REGISTER_MAPS",
            {"_test_connect_heal": low_points},
            clear=False,
        ):
            data = await client.async_read_realtime()
    assert data["grid_frequency"]["value"] == 50.1
    dead.connect.assert_awaited()
    dead.close.assert_called()
    healthy.connect.assert_awaited()
    healthy.read_input_registers.assert_awaited()


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
    # Permanent (non-connection) protocol error still force-disconnects once.
    assert inner.close.call_count >= 1


async def test_not_connected_error_reconnects_and_succeeds():
    """A mid-session 'Not connected' error recreates the client and retries once."""
    from pymodbus.exceptions import ConnectionException

    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    regs = [0] * count
    regs[5035 - start] = 499

    first = MagicMock()
    first.connected = True
    first.connect = AsyncMock(return_value=True)
    first.close = MagicMock()
    first.read_input_registers = AsyncMock(side_effect=ConnectionException("Not connected[AsyncModbusTcpClient]"))

    second = MagicMock()
    second.connected = False
    second.connect = AsyncMock(return_value=True)
    second.close = MagicMock()
    ok = MagicMock()
    ok.isError.return_value = False
    ok.registers = regs
    second.read_input_registers = AsyncMock(return_value=ok)

    cls = MagicMock(side_effect=[first, second])
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1")
        _skip_family_detect(client)
        client.model = "_test_reconnect"
        # Replace the client created in __init__ with our first mock.
        client._client = first  # noqa: SLF001
        with patch.dict(
            "custom_components.sungrow.modbus.REGISTER_MAPS",
            {"_test_reconnect": low_points},
            clear=False,
        ):
            data = await client.async_read_realtime()
    assert data["grid_frequency"]["value"] == 49.9
    first.close.assert_called()
    second.connect.assert_awaited()
    second.read_input_registers.assert_awaited()


async def test_stale_connected_flag_still_reconnects_after_failure():
    """After a connection failure the next poll can open a fresh socket."""
    from pymodbus.exceptions import ConnectionException

    low_points = tuple(p for p in SG_RS_INPUT_POINTS if p.address < 6000)
    start, count = block_bounds(low_points)
    regs = [0] * count
    regs[5035 - start] = 500

    dead = MagicMock()
    dead.connected = True  # stale flag: thinks it's up
    dead.connect = AsyncMock(return_value=True)
    dead.close = MagicMock()
    dead.read_input_registers = AsyncMock(side_effect=ConnectionException("Not connected[x]"))

    healthy = MagicMock()
    healthy.connected = False
    healthy.connect = AsyncMock(return_value=True)
    healthy.close = MagicMock()
    ok = MagicMock()
    ok.isError.return_value = False
    ok.registers = regs
    healthy.read_input_registers = AsyncMock(return_value=ok)

    # __init__ constructs once; recreate after the failed read constructs ``healthy``.
    cls = MagicMock(side_effect=[MagicMock(), healthy])
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("192.168.1.93")
        _skip_family_detect(client)
        client.model = "_test_stale"
        client._client = dead  # noqa: SLF001
        with patch.dict(
            "custom_components.sungrow.modbus.REGISTER_MAPS",
            {"_test_stale": low_points},
            clear=False,
        ):
            data = await client.async_read_realtime()
    assert data["grid_frequency"]["value"] == 50.0


def test_decode_omits_zero_optional_channels():
    """Optional phase/MPPT channels that report 0 are omitted (unavailable in HA)."""
    points = (
        ModbusPoint(0, "phase_b_voltage", "u16", 0.1, "V", nan_value=0xFFFF, omit_zero=True),
        ModbusPoint(1, "mppt1_current", "u16", 0.1, "A"),  # zero is legitimate at night
    )
    out = decode_registers(points, 0, [0, 0])
    assert "phase_b_voltage" not in out
    assert out["mppt1_current"]["value"] == 0.0


async def test_unsupported_address_block_is_skipped_but_other_blocks_return_data():
    """An Illegal Data Address (exception_code=2) block is skipped; other blocks are decoded."""
    test_points = (
        ModbusPoint(0, "ok", "u16", 1),
        # Far enough away to force a second read block.
        ModbusPoint(300, "also_unsupported", "u16", 1),
    )
    cls, inner = _mock_client_cls([], is_error=True)

    def side_effect(address, count, device_id):
        result = MagicMock()
        result.isError.return_value = True
        if address == 0:
            result.__str__ = lambda _s: "ExceptionResponse(dev_id=1, function_code=132, exception_code=2)"
        else:
            result.__str__ = lambda _s: "ExceptionResponse(dev_id=1, function_code=132, exception_code=4)"
        return result

    inner.read_input_registers = AsyncMock(side_effect=side_effect)
    with patch("custom_components.sungrow.modbus.AsyncModbusTcpClient", cls):
        client = SungrowModbusClient("10.0.0.1")
        _skip_family_detect(client)
        client.model = "_test_skip"
        with (
            patch.dict(
                "custom_components.sungrow.modbus.REGISTER_MAPS",
                {"_test_skip": test_points},
                clear=False,
            ),
            pytest.raises(SungrowModbusError, match="exception_code=4"),
        ):
            await client.async_read_realtime()
    # The illegal-address block was skipped; the second block raised a different error.
    assert inner.read_input_registers.await_count == 2


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
