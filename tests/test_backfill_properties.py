"""Property-based tests for the Backfill pure-logic core.

Feature: backfill-historical-statistics.

Each test exercises one of the design's Correctness Properties across a wide input
space with Hypothesis (minimum 100 examples). The pure functions under test carry no
I/O, which makes them an ideal property-test surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.sungrow.backfill import (
    HistoryWindow,
    InvalidRangeError,
    MinuteRow,
    batch_points,
    build_hourly_statistics,
    chunk_time_window,
    resolve_window,
)
from custom_components.sungrow.const import (
    DEFAULT_BACKFILL_DAYS,
    MAX_BACKFILL_DAYS,
)
from custom_components.sungrow.energy_units import normalize_energy_point

UTC = UTC
_WH_UNITS = {"wh", "w·h", "w.h", "watt-hour", "watt hour", "watthour"}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def aware_datetimes(draw, min_year: int = 2021, max_year: int = 2030) -> datetime:
    """Timezone-aware UTC datetimes in a bounded range (avoids timedelta overflow)."""
    naive = draw(
        st.datetimes(
            min_value=datetime(min_year, 1, 1),
            max_value=datetime(max_year, 1, 1),
        )
    )
    return naive.replace(tzinfo=UTC)


@st.composite
def minute_rows(draw) -> list[MinuteRow]:
    """Minute rows anchored to a fixed base: monotonic-ish readings plus noise/resets."""
    base = datetime(2024, 1, 1, tzinfo=UTC)
    entries = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=6000),  # minute offset
                st.floats(
                    min_value=0.0,
                    max_value=1_000_000.0,
                    allow_nan=False,
                    allow_infinity=False,
                ),
            ),
            max_size=40,
        )
    )
    return [MinuteRow(timestamp=base + timedelta(minutes=o), value=v) for o, v in entries]


# ---------------------------------------------------------------------------
# Helpers (fake recorder store + chunked aggregation, hour-aligned)
# ---------------------------------------------------------------------------


def _hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _hour_aligned_chunks(rows: list[MinuteRow], hours_per_chunk: int) -> list[list[MinuteRow]]:
    """Partition rows into chunks whose boundaries fall on hour boundaries.

    Each whole hour is fully contained in exactly one chunk, mirroring how the engine
    splits the window into Time_Chunks without ever splitting a single hour.
    """
    if not rows:
        return []
    by_hour: dict[datetime, list[MinuteRow]] = {}
    for r in rows:
        by_hour.setdefault(_hour(r.timestamp), []).append(r)
    hours = sorted(by_hour)
    chunks: list[list[MinuteRow]] = []
    for i in range(0, len(hours), hours_per_chunk):
        group: list[MinuteRow] = []
        for h in hours[i : i + hours_per_chunk]:
            group.extend(by_hour[h])
        chunks.append(group)
    return chunks


def _aggregate_pieces(rows: list[MinuteRow], hours_per_chunk: int) -> list[list[dict]]:
    """Aggregate hour-aligned chunks in order, carrying running_sum and prev_value."""
    running = 0.0
    prev: float | None = None
    pieces: list[list[dict]] = []
    for chunk in _hour_aligned_chunks(rows, hours_per_chunk):
        data, running = build_hourly_statistics(chunk, "energy", running_sum=running, prev_value=prev)
        pieces.append(data)
        ordered = sorted(chunk, key=lambda r: r.timestamp)
        if ordered:
            prev = ordered[-1].value
    return pieces


def _import_into(store: dict, statistic_id: str, data: list[dict]) -> None:
    """Mimic the recorder helpers: overwrite by (statistic_id, start_hour)."""
    for d in data:
        store[(statistic_id, d["start"])] = d


# ---------------------------------------------------------------------------
# Property 1: Window resolution is bounded and honors configuration
# ---------------------------------------------------------------------------


# Feature: backfill-historical-statistics, Property 1: Window resolution is bounded
# and honors configuration.
# Validates: Requirements 2.3, 3.1, 3.2, 3.3, 3.4, 3.5
@settings(max_examples=100)
@given(
    now=aware_datetimes(),
    option_days=st.one_of(st.none(), st.integers(min_value=-10, max_value=400)),
    start=st.one_of(st.none(), aware_datetimes(2020, 2031)),
)
def test_property1_window_resolution(now, option_days, start):
    # 3.5: a start later than now is an invalid range.
    if start is not None and start > now:
        with pytest.raises(InvalidRangeError):
            resolve_window(now=now, option_days=option_days, start_override=start)
        return

    window = resolve_window(now=now, option_days=option_days, start_override=start)

    # 3.4: finite, positive-length window ending at now.
    assert window.end == now
    assert window.start < window.end
    # 3.3/3.4: length is bounded to [1, MAX] days.
    assert timedelta(days=1) <= (window.end - window.start) <= timedelta(days=MAX_BACKFILL_DAYS)

    if start is None:
        # 3.1/3.2/3.3: default or configured length, clamped.
        requested = DEFAULT_BACKFILL_DAYS if option_days is None else option_days
        clamped = max(1, min(requested, MAX_BACKFILL_DAYS))
        assert window.start == now - timedelta(days=clamped)
    else:
        # 2.3/3.5: honor the explicit start, subject to the same clamp.
        earliest = now - timedelta(days=MAX_BACKFILL_DAYS)
        latest = now - timedelta(days=1)
        assert window.start == min(max(start, earliest), latest)


# ---------------------------------------------------------------------------
# Property 2: Point batching preserves all points and never exceeds the cap
# ---------------------------------------------------------------------------


# Feature: backfill-historical-statistics, Property 2: Point batching preserves all
# points and never exceeds the cap.
# Validates: Requirements 4.1
@settings(max_examples=100)
@given(
    points=st.lists(st.integers(), max_size=200),
    max_size=st.integers(min_value=1, max_value=50),
)
def test_property2_point_batching(points, max_size):
    batches = batch_points(points, max_size=max_size)

    # Cap respected, and no empty batches emitted.
    for batch in batches:
        assert 0 < len(batch) <= max_size
    # Order-preserving: in-order concatenation equals the original list.
    flattened = [p for batch in batches for p in batch]
    assert flattened == points


# ---------------------------------------------------------------------------
# Property 3: Time chunking covers the window in ascending, bounded,
# non-overlapping pieces
# ---------------------------------------------------------------------------


@st.composite
def _windows_and_chunks(draw):
    now = draw(aware_datetimes())
    span = draw(st.integers(min_value=0, max_value=6000))  # minutes
    chunk_minutes = draw(st.integers(min_value=5, max_value=6000))
    window = HistoryWindow(start=now - timedelta(minutes=span), end=now)
    return window, timedelta(minutes=chunk_minutes)


# Feature: backfill-historical-statistics, Property 3: Time chunking covers the window
# in ascending, bounded, non-overlapping pieces.
# Validates: Requirements 4.2, 4.3, 4.4
@settings(max_examples=100)
@given(data=_windows_and_chunks())
def test_property3_time_chunking(data):
    window, chunk = data
    chunks = chunk_time_window(window, chunk)

    if window.start == window.end:
        assert chunks == []
        return

    # Exact coverage of [start, end).
    assert chunks[0][0] == window.start
    assert chunks[-1][1] == window.end

    for start, end in chunks:
        assert start < end  # non-empty
        assert end - start <= chunk  # duration bound (4.2)

    for (s0, e0), (s1, _e1) in zip(chunks, chunks[1:], strict=False):
        assert e0 == s1  # contiguous and non-overlapping (4.4)
        assert s0 < s1  # ascending (4.4)


# ---------------------------------------------------------------------------
# Property 4: Cumulative energy sum is non-decreasing across the whole window
# ---------------------------------------------------------------------------


# Feature: backfill-historical-statistics, Property 4: Cumulative energy sum is
# non-decreasing across the whole window.
# Validates: Requirements 4.5, 7.1
@settings(max_examples=100)
@given(rows=minute_rows(), hours_per_chunk=st.integers(min_value=1, max_value=5))
def test_property4_non_decreasing_sum(rows, hours_per_chunk):
    # Aggregate across chunks with running_sum carried forward, then order by hour.
    all_data = [d for piece in _aggregate_pieces(rows, hours_per_chunk) for d in piece]
    all_data.sort(key=lambda d: d["start"])
    sums = [d["sum"] for d in all_data]

    for earlier, later in zip(sums, sums[1:], strict=False):
        assert earlier <= later + 1e-6


# ---------------------------------------------------------------------------
# Property 5: Aggregation is deterministic and import is idempotent and hour-local
# ---------------------------------------------------------------------------


# Feature: backfill-historical-statistics, Property 5: Aggregation is deterministic and
# import is idempotent and hour-local.
# Validates: Requirements 6.1, 6.2, 6.3, 6.4
@settings(max_examples=100)
@given(rows=minute_rows(), hours_per_chunk=st.integers(min_value=1, max_value=5))
def test_property5_determinism_and_idempotent_import(rows, hours_per_chunk):
    statistic_id = "sungrow:plant_total_yield"

    # Deterministic: equal input yields equal output.
    data_a, _ = build_hourly_statistics(rows, "energy")
    data_b, _ = build_hourly_statistics(rows, "energy")
    assert data_a == data_b

    # 6.3: exactly one entry per hour, every start on an exact UTC hour boundary.
    starts = [d["start"] for d in data_a]
    assert len(starts) == len(set(starts))
    for start in starts:
        assert start.tzinfo is not None
        assert (start.minute, start.second, start.microsecond) == (0, 0, 0)

    # 6.1: all-at-once import equals chunk-by-chunk import.
    store_all: dict = {}
    _import_into(store_all, statistic_id, data_a)

    pieces = _aggregate_pieces(rows, hours_per_chunk)
    store_chunked: dict = {}
    for piece in pieces:
        _import_into(store_chunked, statistic_id, piece)
    assert store_chunked == store_all

    # 6.2: running the whole import twice leaves the stored map unchanged.
    store_twice = dict(store_all)
    _import_into(store_twice, statistic_id, data_a)
    assert store_twice == store_all

    # 6.4: retrying any single chunk re-imports only that chunk's hours and leaves
    # the final stored map identical (hour-local, overwrite-not-append).
    for k in range(len(pieces)):
        partial: dict = {}
        for j, piece in enumerate(pieces):
            if j != k:
                _import_into(partial, statistic_id, piece)
        _import_into(partial, statistic_id, pieces[k])  # retry chunk k
        assert partial == store_all


# ---------------------------------------------------------------------------
# Property 6: Energy unit conversion is correct and stable
# ---------------------------------------------------------------------------


# Feature: backfill-historical-statistics, Property 6: Energy unit conversion is correct
# and stable.
# Validates: Requirements 7.5, 7.6
@settings(max_examples=100)
@given(
    value=st.floats(min_value=0.0, max_value=1_000_000_000.0, allow_nan=False, allow_infinity=False),
    unit=st.sampled_from(["Wh", "wh", "W·h", "watthour", "kWh", "W", "kW", "%", "V", ""]),
)
def test_property6_unit_conversion(value, unit):
    out = normalize_energy_point({"value": value, "unit": unit})

    if unit.lower() in _WH_UNITS:
        # Wh -> kWh with 3-decimal rounding.
        assert out["unit"] == "kWh"
        assert out["value"] == round(value / 1000.0, 3)
    else:
        # kWh or non-energy units: value and unit unchanged.
        assert out["value"] == value
        assert out["unit"] == unit


# ---------------------------------------------------------------------------
# Property 7: Statistic_Ids are scoped per plant and never collide
# ---------------------------------------------------------------------------


from custom_components.sungrow.backfill import build_series_target  # noqa: E402


# Feature: backfill-historical-statistics, Property 7: Statistic_Ids are scoped per
# plant and never collide.
# Validates: Requirements 9.2
@settings(max_examples=100)
@given(
    plant_a=st.text(alphabet=st.characters(min_codepoint=48, max_codepoint=122), min_size=1, max_size=12),
    plant_b=st.text(alphabet=st.characters(min_codepoint=48, max_codepoint=122), min_size=1, max_size=12),
    code=st.text(alphabet=st.characters(min_codepoint=48, max_codepoint=122), min_size=1, max_size=20),
    kind=st.sampled_from(["energy", "power"]),
)
def test_property7_statistic_id_scoping(plant_a, plant_b, code, kind):
    target_a = build_series_target(plant_id=plant_a, point_code=code, kind=kind, entity_id=None, unit=None)
    target_b = build_series_target(plant_id=plant_b, point_code=code, kind=kind, entity_id=None, unit=None)

    # External ids always carry the sungrow: scope prefixed by the plant id.
    assert target_a.is_external and target_b.is_external
    assert target_a.statistic_id == f"sungrow:{plant_a}_{code}"
    assert target_b.statistic_id == f"sungrow:{plant_b}_{code}"

    # Distinct plants for the same code never collide.
    if plant_a != plant_b:
        assert target_a.statistic_id != target_b.statistic_id
