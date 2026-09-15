"""Property-based tests over every local-Modbus family register map (#438).

Register decoding has a wide surface for silent errors — 32-bit low-word-first
ordering, signed vs unsigned, per-point scales, NAN sentinels and ``omit_zero`` —
spread across several family maps. The hand-written-frame tests in
``test_modbus.py`` catch regressions in *known* cases (a realistic SG-RS frame),
but not a wrong scale or a flipped word order on a point that nobody happens to
have a fixture for. That blind spot is the class of bug behind #400 (a lifetime
total mis-scaled as "today") and the duplicate-code collisions in #427.

These tests instead assert three invariants over **every numeric point in every
family map** in :data:`REGISTER_MAPS`, parametrised so a family added by a future
community contribution is covered automatically:

1. **round-trip** — encoding a value into raw register words per the point's spec
   (width, signedness, low-word-first order) and decoding it again returns the
   original, respecting the point's scale. This pins the word order and the
   sign/width handling: flip either in :func:`_combine` and the round-trip breaks.
2. **bounds** — any ``u16``/``s16``/``u32``/``s32`` payload decodes to a finite
   value; a payload equal to the point's NAN sentinel is omitted rather than
   surfaced, and an ``omit_zero`` point drops a raw zero.
3. **scale-sanity** — no point's scale lets a plausible raw reading decode to a
   value whose magnitude is outside a physically sensible envelope for its unit
   (a 10x scale slip on a voltage/temperature/frequency register is caught here,
   because round-trip alone cannot — it applies the same scale on both sides).

The decode entry point under test is
:func:`custom_components.sungrow.modbus_registers.decode_registers`, the same
function the live Modbus client calls (``modbus.py`` → ``decode_registers``).
Each point is decoded on its own single-point block (``block_start =
point.address``) so a family's deliberate duplicate codes (grid_frequency
5035/5241, total_active_power 13033 — see #427) can't mask one another here;
that precedence is pinned separately in ``test_modbus.py``.

Two cheap, easy-to-get-wrong invariants are guarded rather than property-tested:
every family contributes numeric points, and every NAN sentinel matches its
point's register width — the latter being the bug behind #401, which the
properties above cannot see.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from custom_components.sungrow.modbus_registers import (
    REGISTER_MAPS,
    ModbusPoint,
    decode_registers,
)

# ---------------------------------------------------------------------------
# Type model — must mirror ``_combine`` in modbus_registers.py.
# ---------------------------------------------------------------------------
_NUMERIC_TYPES: frozenset[str] = frozenset({"u16", "s16", "u32", "s32"})
_TYPE_BITS: dict[str, int] = {"u16": 16, "s16": 16, "u32": 32, "s32": 32}
_SIGNED_TYPES: frozenset[str] = frozenset({"s16", "s32"})

# The canonical "not available" sentinel per numeric type — all-ones for unsigned,
# max-positive for signed. A point must carry the sentinel for its own width: the
# decode path compares the *combined* value, so a 32-bit point holding a 16-bit
# sentinel is never omitted (the two can't be equal).
_CANONICAL_NAN: dict[str, int] = {
    "u16": 0xFFFF,
    "s16": 0x7FFF,
    "u32": 0xFFFFFFFF,
    "s32": 0x7FFFFFFF,
}


def _raw_range(data_type: str) -> tuple[int, int]:
    """Return the inclusive ``(min, max)`` integer range for a numeric data type."""
    bits = _TYPE_BITS[data_type]
    if data_type in _SIGNED_TYPES:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


def _encode_raw(raw: int, data_type: str) -> list[int]:
    """Encode a decoded integer into register words — the inverse of ``_combine``.

    16-bit types occupy one register; 32-bit types occupy two, **low word first**.
    Negative values are stored two's-complement, exactly as the inverter puts them
    on the wire, so decoding these words has to return ``raw`` unchanged.
    """
    bits = _TYPE_BITS[data_type]
    unsigned = raw & ((1 << bits) - 1)  # two's-complement wrap for signed types
    if bits == 16:
        return [unsigned]
    return [unsigned & 0xFFFF, (unsigned >> 16) & 0xFFFF]


def _is_omitted(point: ModbusPoint, raw: int) -> bool:
    """Whether ``decode_registers`` drops this raw value (NAN sentinel / omit_zero)."""
    if point.nan_value is not None and raw == point.nan_value:
        return True
    return bool(point.omit_zero and raw == 0)


# All (family, point) pairs for numeric points across every family map. Iterating
# ``REGISTER_MAPS`` means a new family (or a new point in an existing family) is
# covered the moment it is added to the map — no test edit required.
_NUMERIC_POINTS: list[tuple[str, ModbusPoint]] = [
    (family, point) for family, points in REGISTER_MAPS.items() for point in points if point.data_type in _NUMERIC_TYPES
]

_POINT_IDS: list[str] = [
    f"{family}:{point.code}@{point.address}:{point.data_type}" for family, point in _NUMERIC_POINTS
]


# ---------------------------------------------------------------------------
# Physically-sane magnitude ceilings per unit (property 3).
# ---------------------------------------------------------------------------
# Each ceiling is the largest *magnitude* a decoded reading of that unit could
# plausibly take on any Sungrow residential/commercial device, with headroom for
# the register's own over-provisioning. They sit above every full-scale reading
# the shipped maps can produce at their correct scale, but below the value a
# 10x scale slip would produce on a narrow (16-bit) register — so mis-scaling a
# voltage/temperature/frequency/percentage point pushes a plausible raw reading
# past the ceiling and fails the test. Units not listed (e.g. power_factor, and
# the raw enum/identity codes with unit ``None``) have no physical envelope and
# are skipped.
_UNIT_ABS_CEILING: dict[str, float] = {
    "V": 10_000.0,  # correct full-scale: u16*0.1 = 6553.5
    "A": 100_000.0,  # correct full-scale: u16*1 (BMS current) = 65535
    "°C": 5_000.0,  # correct full-scale: s16*0.1 = 3276.7
    "Hz": 10_000.0,  # correct full-scale: u16*0.1 = 6553.5
    "%": 10_000.0,  # correct full-scale: u16*0.1 = 6553.5
    "kWh": 5_000_000_000.0,  # correct full-scale: u32*1 = 4.29e9
    "W": 5_000_000_000.0,  # correct full-scale: u32*1 = 4.29e9
}

_SCALED_POINTS: list[tuple[str, ModbusPoint]] = [
    (family, point) for family, point in _NUMERIC_POINTS if point.unit in _UNIT_ABS_CEILING
]
_SCALED_IDS: list[str] = [f"{family}:{point.code}@{point.address}:{point.unit}" for family, point in _SCALED_POINTS]


# ---------------------------------------------------------------------------
# Property 1 — round-trip.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("family", "point"), _NUMERIC_POINTS, ids=_POINT_IDS)
@given(data=st.data())
@settings(max_examples=100, deadline=None, derandomize=True)
def test_numeric_point_round_trips(family: str, point: ModbusPoint, data: st.DataObject) -> None:
    """Encoding a drawn value and decoding it returns the original (within scale precision).

    The raw integer is encoded into register words per the point's width, sign and
    low-word-first order, then decoded through the production path. The decoded
    value must equal ``round(raw * scale, 3)`` — the exact transform
    ``decode_registers`` applies — so a wrong word order, sign extension or
    register width surfaces as a mismatch here.
    """
    lo, hi = _raw_range(point.data_type)
    raw = data.draw(st.integers(min_value=lo, max_value=hi))
    # Sentinel / omit_zero values are *meant* to be dropped, so they have nothing
    # to round-trip to; exclude them here (property 2 asserts they are dropped).
    assume(not _is_omitted(point, raw))

    words = _encode_raw(raw, point.data_type)
    out = decode_registers((point,), point.address, words)

    assert point.code in out, f"{family}:{point.code} was dropped for raw={raw}"
    assert out[point.code]["value"] == round(raw * point.scale, 3)
    assert out[point.code]["unit"] == point.unit
    assert out[point.code]["source"] == "modbus"


# ---------------------------------------------------------------------------
# Property 2 — bounds / sentinel omission.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("family", "point"), _NUMERIC_POINTS, ids=_POINT_IDS)
@given(data=st.data())
@settings(max_examples=100, deadline=None, derandomize=True)
def test_numeric_point_bounds_and_omission(family: str, point: ModbusPoint, data: st.DataObject) -> None:
    """Any payload decodes to a finite value; sentinels and omit_zero are dropped.

    For every representable raw value: if it is the NAN sentinel (or a zero on an
    ``omit_zero`` point) the point must be absent from the decode output; otherwise
    the point must be present with a finite numeric value — never NaN/inf, never a
    surfaced sentinel.
    """
    lo, hi = _raw_range(point.data_type)
    raw = data.draw(st.integers(min_value=lo, max_value=hi))

    out = decode_registers((point,), point.address, _encode_raw(raw, point.data_type))

    if _is_omitted(point, raw):
        assert point.code not in out, f"{family}:{point.code} surfaced sentinel/zero raw={raw}"
        return

    assert point.code in out, f"{family}:{point.code} dropped a valid raw={raw}"
    value = out[point.code]["value"]
    assert isinstance(value, (int, float))
    assert math.isfinite(value), f"{family}:{point.code} decoded non-finite {value} for raw={raw}"


# ---------------------------------------------------------------------------
# Property 3 — scale sanity.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("family", "point"), _SCALED_POINTS, ids=_SCALED_IDS)
@given(data=st.data())
@settings(max_examples=100, deadline=None, derandomize=True)
def test_scale_keeps_readings_in_physical_range(family: str, point: ModbusPoint, data: st.DataObject) -> None:
    """No plausible raw reading decodes outside the unit's physical magnitude ceiling.

    A raw value drawn across the register's representable range, once scaled, must
    stay within :data:`_UNIT_ABS_CEILING` for the point's unit. A scale that is (say)
    10x too large on a 16-bit voltage/temperature/frequency register makes the
    register's full-scale reading blow past the ceiling — the failure mode round-trip
    cannot see, because it applies the same (wrong) scale on both sides.
    """
    ceiling = _UNIT_ABS_CEILING[point.unit]  # type: ignore[index]  # filtered to known units
    lo, hi = _raw_range(point.data_type)
    raw = data.draw(st.integers(min_value=lo, max_value=hi))
    assume(not _is_omitted(point, raw))

    out = decode_registers((point,), point.address, _encode_raw(raw, point.data_type))

    assert point.code in out
    magnitude = abs(out[point.code]["value"])
    assert magnitude <= ceiling, (
        f"{family}:{point.code} scale {point.scale} maps raw={raw} to {out[point.code]['value']} "
        f"{point.unit}, outside the sane ±{ceiling} envelope"
    )


# ---------------------------------------------------------------------------
# Guard: the parametrised suite actually covers every shipped family.
# ---------------------------------------------------------------------------
def test_every_family_contributes_numeric_points() -> None:
    """Each family map in REGISTER_MAPS contributes at least one numeric point.

    Cheap insurance that the parametrisation enumerates families rather than
    silently collapsing to one — so a newly-registered family is exercised.
    """
    covered = {family for family, _ in _NUMERIC_POINTS}
    assert covered == set(REGISTER_MAPS), f"families missing numeric coverage: {set(REGISTER_MAPS) - covered}"
    assert _SCALED_POINTS, "no unit-bearing points found for the scale-sanity property"


# ---------------------------------------------------------------------------
# Guard: every NAN sentinel matches its point's register width (#401).
# ---------------------------------------------------------------------------
_POINTS_WITH_NAN: list[tuple[str, ModbusPoint]] = [
    (family, point) for family, point in _NUMERIC_POINTS if point.nan_value is not None
]
_NAN_IDS: list[str] = [f"{family}:{point.code}@{point.address}:{point.data_type}" for family, point in _POINTS_WITH_NAN]


@pytest.mark.parametrize(("family", "point"), _POINTS_WITH_NAN, ids=_NAN_IDS)
def test_nan_sentinel_matches_the_register_width(family: str, point: ModbusPoint) -> None:
    """A point's NAN sentinel must be the sentinel for its *own* register width.

    The decode path compares the combined value against ``nan_value``, so a width
    mismatch is silently wrong in both directions: an unsupported 32-bit register
    (``0xFFFFFFFF``) is never omitted and surfaces as an absurd reading, while a real
    reading that happens to equal the 16-bit sentinel (``65535``) is dropped as if the
    hardware did not support the point.

    This is the bug behind #401: ``total_imported_energy`` and ``total_exported_energy``
    are declared ``u32`` but carried the ``u16`` sentinel. Round-trip cannot see it
    (both sides use the same ``nan_value``), which is why it needed its own invariant.
    """
    assert point.nan_value == _CANONICAL_NAN[point.data_type], (
        f"{family}:{point.code} is {point.data_type} but its NAN sentinel is "
        f"{point.nan_value:#x}, not {_CANONICAL_NAN[point.data_type]:#x}"
    )
