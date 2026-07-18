"""Property-based tests for the local Modbus register partitioner (#318).

The partitioner turns a sparse register map into a small number of contiguous
reads. Two invariants must hold for *any* possible map — not just the ones we
ship today — so a future family added by community contribution cannot
reintroduce the 243-register block that pymodbus rejects with
``1 < count 243 < 125 !``:

1. Every emitted block fits within the Modbus function-4 protocol cap
   (:data:`MODBUS_MAX_READ_COUNT` = 125).
2. Every input point is covered by exactly one block — the partitioner
   never silently drops a register.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.sungrow.modbus_registers import (
    MODBUS_MAX_READ_COUNT,
    ModbusPoint,
    block_partitions,
)

# u16/s16 occupy 1 register, u32/s32 occupy 2. Restricting the address range
# well below 65535 keeps u32 points from running off the end of the space.
_DATA_TYPES = ("u16", "s16", "u32", "s32")


@st.composite
def _modbus_points(draw):
    """A small, well-formed set of ModbusPoints with unique addresses."""
    addresses = draw(
        st.lists(
            st.integers(min_value=0, max_value=60_000),
            min_size=1,
            max_size=120,
            unique=True,
        )
    )
    return tuple(ModbusPoint(address, f"p_{address}", draw(st.sampled_from(_DATA_TYPES)), 1) for address in addresses)


@given(points=_modbus_points())
@settings(max_examples=200, deadline=None)
def test_block_partitions_never_exceeds_modbus_cap(points):
    """No emitted block can be larger than the Modbus per-request register cap."""
    blocks = block_partitions(points)
    for start, count in blocks:
        assert 1 <= count <= MODBUS_MAX_READ_COUNT, f"block ({start}, {count}) exceeds Modbus cap"


@given(points=_modbus_points())
@settings(max_examples=200, deadline=None)
def test_block_partitions_covers_every_point(points):
    """Every input point's register range lies fully inside exactly one block."""
    blocks = block_partitions(points)
    for point in points:
        point_end = point.address + point.register_count
        covering = [(s, c) for s, c in blocks if s <= point.address and point_end <= s + c]
        assert len(covering) == 1, f"point {point.code}@{point.address} covered by {covering}"


@given(
    points=_modbus_points(),
    # min=2 because a single u32 point is irreducibly 2 registers wide and no
    # partitioner can shrink it below that; useful real-world caps are 100..125.
    cap=st.integers(min_value=2, max_value=500),
)
@settings(max_examples=100, deadline=None)
def test_block_partitions_respects_caller_supplied_cap(points, cap):
    """A caller-supplied ``max_block_size`` is honoured up to the Modbus protocol max.

    ``max_block_size`` is clamped to :data:`MODBUS_MAX_READ_COUNT`, so any block
    must fit under ``min(cap, MODBUS_MAX_READ_COUNT)`` — never larger, whatever
    the caller asks for.
    """
    effective_cap = min(cap, MODBUS_MAX_READ_COUNT)
    blocks = block_partitions(points, max_block_size=cap)
    for start, count in blocks:
        assert count <= effective_cap, f"block ({start}, {count}) exceeds requested cap {effective_cap}"
