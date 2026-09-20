"""Tests for software-derived daily energy from a lifetime counter (#223, #471)."""

from datetime import date

from custom_components.sungrow.daily_yield import (
    DailyYieldBaseline,
    DerivedDailyEnergyState,
    apply_derived_daily_grid_energy,
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


# ---------------------------------------------------------------------------
# Software-derived daily grid import/export (#471)
# ---------------------------------------------------------------------------

_DAY = date(2026, 9, 20)


def _point(value, unit="kWh"):
    return {"code": "x", "value": value, "unit": unit, "source": "modbus"}


def _seeded(code="total_imported_energy", baseline=6462.0, day=_DAY):
    """Grid state as it looks after today's first sample was taken."""
    return DerivedDailyEnergyState(
        baselines={code: DailyYieldBaseline(baseline=baseline, baseline_date=day, last_total=baseline)}
    )


def test_grid_daily_derived_when_register_absent():
    """No daily register at all → today's import is total − start-of-day (#471)."""
    data = {"total_imported_energy": _point(6470.0)}

    out, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=_seeded())

    assert derived == {"daily_imported_energy": 8.0}
    assert out["daily_imported_energy"]["value"] == 8.0
    assert out["daily_imported_energy"]["source"] == "modbus_derived"
    assert out["daily_imported_energy"]["unit"] == "kWh"
    # The lifetime counter is left alone, and the baseline advances to it.
    assert out["total_imported_energy"]["value"] == 6470.0
    assert state.baselines["total_imported_energy"].last_total == 6470.0


def test_grid_daily_replaces_flat_zero_register():
    """The SH flat-0 daily register (#401) is filled in from the lifetime counter."""
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(0.0)}

    out, _, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=_seeded())

    assert derived == {"daily_imported_energy": 8.0}
    assert out["daily_imported_energy"]["value"] == 8.0
    assert out["daily_imported_energy"]["source"] == "modbus_derived"


def test_grid_daily_replaces_live_register_and_advances_baseline():
    """Once a lifetime counter exists the derived value takes over, so the source is stable.

    The sensor platform picks the device/state class once, when the entity is built, so a
    value that alternated between the raw register and our arithmetic would be classified
    by whenever Home Assistant last restarted.
    """
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(3.5)}

    out, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=_seeded())

    assert derived == {"daily_imported_energy": 8.0}
    assert out["daily_imported_energy"]["value"] == 8.0
    assert out["daily_imported_energy"]["source"] == "modbus_derived"
    # The device's own figure rides along so the two can be compared from the UI.
    assert out["daily_imported_energy"]["raw_register_value"] == 3.5
    assert state.baselines["total_imported_energy"].last_total == 6470.0


def test_grid_daily_omits_raw_attribute_when_the_register_was_absent():
    """Nothing to compare against → no ``raw_register_value`` attribute."""
    out, _, _ = apply_derived_daily_grid_energy(
        {"total_imported_energy": _point(6470.0)}, local_date=_DAY, state=_seeded()
    )

    assert "raw_register_value" not in out["daily_imported_energy"]


def test_grid_daily_first_sample_seed_ignored_when_the_day_is_the_whole_counter():
    """``raw == total`` implies a start-of-day baseline of 0, which must not be trusted.

    A 0 baseline is indistinguishable from a glitched one and would hand back the whole
    lifetime total as "today" (#400), so the day reads 0 instead.
    """
    data = {"total_imported_energy": _point(12.4), "daily_imported_energy": _point(12.4)}

    _, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 0.0}
    assert state.baselines["total_imported_energy"].baseline == 12.4


def test_grid_daily_seeds_when_the_stored_entry_carries_no_history():
    """A partial/corrupt store entry is treated as no history, so it still seeds."""
    state = DerivedDailyEnergyState(baselines={"total_imported_energy": DailyYieldBaseline()})
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(12.4)}

    _, new_state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=state)

    assert derived == {"daily_imported_energy": 12.4}
    assert round(new_state.baselines["total_imported_energy"].baseline, 3) == 6457.6


def test_grid_daily_first_sample_is_seeded_from_the_live_register():
    """Taking over mid-day must not throw away the part of the day already counted.

    Without a seed the baseline anchors at the current total, so the day we switch would
    read 0 until the next midnight. The device's own figure tells us where midnight was.
    """
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(12.4)}

    out, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 12.4}
    # 6470 − 12.4: the implied start-of-day total.
    assert round(state.baselines["total_imported_energy"].baseline, 3) == 6457.6
    assert out["daily_imported_energy"]["value"] == 12.4


def test_grid_daily_first_sample_seed_is_ignored_when_implausible():
    """A reading above the lifetime counter tells us nothing; fall back to a 0 day."""
    data = {"total_imported_energy": _point(100.0), "daily_imported_energy": _point(500.0)}

    _, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 0.0}
    assert state.baselines["total_imported_energy"].baseline == 100.0


def test_grid_daily_first_sample_flat_zero_register_starts_the_day_at_zero():
    """A flat-0 register carries no start-of-day information (#401), so the day starts at 0."""
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(0.0)}

    _, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 0.0}
    assert state.baselines["total_imported_energy"].baseline == 6470.0


def test_grid_daily_seed_is_not_reused_once_there_is_history():
    """Only a genuinely fresh start seeds from the register; later days anchor on yesterday."""
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DailyYieldBaseline(
                baseline=6400.0, baseline_date=date(2026, 9, 19), last_total=6462.0
            )
        }
    )
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(99.0)}

    _, new_state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=state)

    # Anchored on yesterday's last total (6462), not on the bogus 99 the register reports.
    assert derived == {"daily_imported_energy": 8.0}
    assert new_state.baselines["total_imported_energy"].baseline == 6462.0


def test_grid_daily_silent_without_lifetime_total():
    """A meterless plant publishes nothing rather than a fabricated 0 (#387 contract)."""
    out, state, derived = apply_derived_daily_grid_energy({}, local_date=_DAY, state=DerivedDailyEnergyState())

    assert out == {}
    assert derived == {}
    assert state.baselines == {}


def test_grid_daily_midnight_rollover_anchors_on_yesterdays_last_total():
    """A new local day starts from where the counter was at the previous day's end."""
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DailyYieldBaseline(
                baseline=6400.0, baseline_date=date(2026, 9, 19), last_total=6462.0
            )
        }
    )

    _, new_state, derived = apply_derived_daily_grid_energy(
        {"total_imported_energy": _point(6465.0)}, local_date=_DAY, state=state
    )

    assert derived["daily_imported_energy"] == 3.0
    assert new_state.baselines["total_imported_energy"].baseline == 6462.0
    assert new_state.baselines["total_imported_energy"].baseline_date == _DAY


def test_grid_daily_counters_track_independent_baselines():
    """Import and export cross midnight independently, so each keeps its own baseline."""
    data = {"total_imported_energy": _point(6470.0), "total_exported_energy": _point(2005.0)}

    out, state, derived = apply_derived_daily_grid_energy(data, local_date=_DAY, state=_seeded())

    assert derived == {"daily_imported_energy": 8.0, "daily_exported_energy": 0.0}
    assert out["daily_imported_energy"]["value"] == 8.0
    # Export had no seeded baseline, so today starts at the current lifetime total.
    assert out["daily_exported_energy"]["value"] == 0.0
    assert set(state.baselines) == {"total_imported_energy", "total_exported_energy"}


def test_grid_daily_state_store_roundtrip():
    """Per-counter baselines survive serialize → deserialize."""
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DailyYieldBaseline(baseline=6462.0, baseline_date=_DAY, last_total=6470.0),
            "total_exported_energy": DailyYieldBaseline(baseline=100.0, baseline_date=_DAY, last_total=101.0),
        }
    )
    assert DerivedDailyEnergyState.from_store(state.to_store()) == state
    assert DerivedDailyEnergyState.from_store(None).baselines == {}
    assert DerivedDailyEnergyState.from_store({"total_imported_energy": "nope"}).baselines == {}
