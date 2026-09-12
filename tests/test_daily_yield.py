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


def test_zero_baseline_reanchors_instead_of_reporting_lifetime_total():
    """A 0 baseline must not make daily report the whole lifetime yield (#400).

    The SG5.0RS report showed ``daily_yield`` = 16064 (the lifetime register), which is
    exactly ``total − 0``: a stored baseline of 0 makes the subtraction subtract nothing
    and "today" becomes the plant's entire history.
    """
    state = DailyYieldBaseline(baseline=0.0, baseline_date=date(2026, 8, 2), last_total=0.0)
    daily, new = step_daily_yield(16064.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 16064.0
    assert new.last_total == 16064.0


def test_zero_last_total_at_rollover_reanchors():
    """A day-boundary sample of 0 (firmware reboot) must not anchor the new day at 0."""
    state = DailyYieldBaseline(baseline=6462.0, baseline_date=date(2026, 8, 1), last_total=0.0)
    daily, new = step_daily_yield(16064.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 16064.0
    assert new.baseline_date == date(2026, 8, 2)


def test_negative_baseline_reanchors():
    """A negative stored baseline is unusable too and re-anchors at the current total."""
    state = DailyYieldBaseline(baseline=-5.0, baseline_date=date(2026, 8, 2), last_total=-5.0)
    daily, new = step_daily_yield(100.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 100.0


def test_genuine_zero_plant_recovers_without_absurd_day():
    """A plant genuinely sitting at 0 lifetime recovers sanely, never reporting a lifetime total.

    Trade-off, and the reason this is a documented one-off: the first non-zero sample
    after a real 0 re-anchors (daily 0) rather than crediting the whole jump, because a
    0 baseline is indistinguishable from a glitched one. Growth is measured from that
    re-anchor on, so at most the first day's opening increment is lost — vastly better
    than reporting the entire lifetime yield (#400).
    """
    state = DailyYieldBaseline()
    daily, new = step_daily_yield(0.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 0.0

    daily2, new2 = step_daily_yield(5.0, date(2026, 8, 2), new)
    assert daily2 == 0.0  # re-anchored, not 5.0 reported as "today"
    assert new2.baseline == 5.0

    daily3, _ = step_daily_yield(7.0, date(2026, 8, 2), new2)
    assert daily3 == 2.0  # normal tracking resumes


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
