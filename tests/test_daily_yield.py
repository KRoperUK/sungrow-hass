"""Tests for software-derived daily yield from total_yield (#223)."""

from datetime import date

from custom_components.sungrow.daily_yield import (
    DailyYieldBaseline,
    apply_derived_daily_yield,
    step_daily_yield,
)


def test_first_sample_starts_day_at_zero():
    """With no history, baseline anchors at current total so daily starts at 0."""
    state = DailyYieldBaseline()
    daily, new = step_daily_yield(6462.0, date(2026, 7, 13), state)
    assert daily == 0.0
    assert new.baseline == 6462.0
    assert new.baseline_date == date(2026, 7, 13)
    assert new.last_total == 6462.0


def test_same_day_growth():
    """Within a day, daily tracks total − baseline."""
    state = DailyYieldBaseline(baseline=6462.0, baseline_date=date(2026, 7, 13), last_total=6462.0)
    daily, new = step_daily_yield(6467.0, date(2026, 7, 13), state)
    assert daily == 5.0
    assert new.baseline == 6462.0
    assert new.last_total == 6467.0


def test_midnight_rollover_uses_yesterdays_last_total():
    """On a new local date, baseline becomes the previous sample's total."""
    state = DailyYieldBaseline(baseline=6400.0, baseline_date=date(2026, 7, 12), last_total=6462.0)
    daily, new = step_daily_yield(6465.0, date(2026, 7, 13), state)
    assert new.baseline == 6462.0
    assert new.baseline_date == date(2026, 7, 13)
    assert daily == 3.0


def test_meter_reset_reanchors():
    """If total drops below baseline, re-anchor rather than go negative."""
    state = DailyYieldBaseline(baseline=100.0, baseline_date=date(2026, 7, 13), last_total=150.0)
    daily, new = step_daily_yield(10.0, date(2026, 7, 13), state)
    assert daily == 0.0
    assert new.baseline == 10.0
    assert new.last_total == 10.0


def test_store_roundtrip():
    """Baseline survives serialize → deserialize."""
    state = DailyYieldBaseline(baseline=1.5, baseline_date=date(2026, 7, 13), last_total=2.0)
    restored = DailyYieldBaseline.from_store(state.to_store())
    assert restored == state
    assert DailyYieldBaseline.from_store(None).baseline is None
    assert DailyYieldBaseline.from_store({"baseline_date": "nope"}).baseline_date is None


def test_apply_overwrites_daily_from_total():
    """apply_derived_daily_yield replaces a bogus register daily with the delta."""
    data = {
        "total_yield": {"code": "total_yield", "value": 6467.0, "unit": "kWh", "source": "modbus"},
        "daily_yield": {"code": "daily_yield", "value": 201.6, "unit": "kWh", "source": "modbus"},
    }
    state = DailyYieldBaseline(baseline=6462.0, baseline_date=date(2026, 7, 13), last_total=6462.0)
    out, new_state, daily = apply_derived_daily_yield(data, local_date=date(2026, 7, 13), state=state)
    assert daily == 5.0
    assert out["daily_yield"]["value"] == 5.0
    assert out["daily_yield"]["source"] == "modbus_derived"
    assert out["daily_yield"]["unit"] == "kWh"
    assert new_state.last_total == 6467.0
    # Original total untouched.
    assert out["total_yield"]["value"] == 6467.0


def test_apply_noop_without_total():
    """No total_yield → leave data alone."""
    data = {"daily_yield": {"code": "daily_yield", "value": 1.0, "unit": "kWh", "source": "modbus"}}
    state = DailyYieldBaseline()
    out, new_state, daily = apply_derived_daily_yield(data, local_date=date(2026, 7, 13), state=state)
    assert daily is None
    assert out is data
    assert new_state is state
