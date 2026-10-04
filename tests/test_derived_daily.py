"""Tests for software-derived daily energy from a lifetime counter (#223, #471, #486)."""

from datetime import date

import pytest

from custom_components.sungrow.derived_daily import (
    DERIVED_DAILY_COUNTERS,
    MAX_BATTERY_POWER_W,
    MAX_GRID_POWER_W,
    DerivedDailyBaseline,
    DerivedDailyEnergyState,
    apply_derived_daily_energy,
    apply_derived_daily_yield,
    implausible_counter_jump,
    step_derived_daily,
)


def test_first_sample_starts_day_at_zero():
    """With no history, baseline anchors at current total so daily starts at 0."""
    state = DerivedDailyBaseline()
    daily, new = step_derived_daily(6462.0, date(2026, 7, 13), state)
    assert daily == 0.0
    assert new.baseline == 6462.0
    assert new.baseline_date == date(2026, 7, 13)
    assert new.last_total == 6462.0


def test_same_day_growth():
    """Within a day, daily tracks total − baseline."""
    state = DerivedDailyBaseline(baseline=6462.0, baseline_date=date(2026, 7, 13), last_total=6462.0)
    daily, new = step_derived_daily(6467.0, date(2026, 7, 13), state)
    assert daily == 5.0
    assert new.baseline == 6462.0
    assert new.last_total == 6467.0


def test_midnight_rollover_uses_yesterdays_last_total():
    """On a new local date, baseline becomes the previous sample's total."""
    state = DerivedDailyBaseline(baseline=6400.0, baseline_date=date(2026, 7, 12), last_total=6462.0)
    daily, new = step_derived_daily(6465.0, date(2026, 7, 13), state)
    assert new.baseline == 6462.0
    assert new.baseline_date == date(2026, 7, 13)
    assert daily == 3.0


def test_meter_reset_reanchors():
    """If total drops below baseline, re-anchor rather than go negative."""
    state = DerivedDailyBaseline(baseline=100.0, baseline_date=date(2026, 7, 13), last_total=150.0)
    daily, new = step_derived_daily(10.0, date(2026, 7, 13), state)
    assert daily == 0.0
    assert new.baseline == 10.0
    assert new.last_total == 10.0


def test_zero_baseline_reanchors_instead_of_reporting_lifetime_total():
    """A 0 baseline must not make daily report the whole lifetime yield (#400).

    The SG5.0RS report showed ``daily_yield`` = 16064 (the lifetime register), which is
    exactly ``total − 0``: a stored baseline of 0 makes the subtraction subtract nothing
    and "today" becomes the plant's entire history.
    """
    state = DerivedDailyBaseline(baseline=0.0, baseline_date=date(2026, 8, 2), last_total=0.0)
    daily, new = step_derived_daily(16064.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 16064.0
    assert new.last_total == 16064.0


def test_zero_last_total_at_rollover_reanchors():
    """A day-boundary sample of 0 (firmware reboot) must not anchor the new day at 0."""
    state = DerivedDailyBaseline(baseline=6462.0, baseline_date=date(2026, 8, 1), last_total=0.0)
    daily, new = step_derived_daily(16064.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 16064.0
    assert new.baseline_date == date(2026, 8, 2)


def test_negative_baseline_reanchors():
    """A negative stored baseline is unusable too and re-anchors at the current total."""
    state = DerivedDailyBaseline(baseline=-5.0, baseline_date=date(2026, 8, 2), last_total=-5.0)
    daily, new = step_derived_daily(100.0, date(2026, 8, 2), state)
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
    state = DerivedDailyBaseline()
    daily, new = step_derived_daily(0.0, date(2026, 8, 2), state)
    assert daily == 0.0
    assert new.baseline == 0.0

    daily2, new2 = step_derived_daily(5.0, date(2026, 8, 2), new)
    assert daily2 == 0.0  # re-anchored, not 5.0 reported as "today"
    assert new2.baseline == 5.0

    daily3, _ = step_derived_daily(7.0, date(2026, 8, 2), new2)
    assert daily3 == 2.0  # normal tracking resumes


def test_store_roundtrip():
    """Baseline survives serialize → deserialize."""
    state = DerivedDailyBaseline(baseline=1.5, baseline_date=date(2026, 7, 13), last_total=2.0)
    restored = DerivedDailyBaseline.from_store(state.to_store())
    assert restored == state
    assert DerivedDailyBaseline.from_store(None).baseline is None
    assert DerivedDailyBaseline.from_store({"baseline_date": "nope"}).baseline_date is None


def test_apply_overwrites_daily_from_total():
    """apply_derived_daily_yield replaces a bogus register daily with the delta."""
    data = {
        "total_yield": {"code": "total_yield", "value": 6467.0, "unit": "kWh", "source": "modbus"},
        "daily_yield": {"code": "daily_yield", "value": 201.6, "unit": "kWh", "source": "modbus"},
    }
    state = DerivedDailyBaseline(baseline=6462.0, baseline_date=date(2026, 7, 13), last_total=6462.0)
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
    state = DerivedDailyBaseline()
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
        baselines={code: DerivedDailyBaseline(baseline=baseline, baseline_date=day, last_total=baseline)}
    )


def test_grid_daily_derived_when_register_absent():
    """No daily register at all → today's import is total − start-of-day (#471)."""
    data = {"total_imported_energy": _point(6470.0)}

    out, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=_seeded())

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

    out, _, derived = apply_derived_daily_energy(data, local_date=_DAY, state=_seeded())

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

    out, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=_seeded())

    assert derived == {"daily_imported_energy": 8.0}
    assert out["daily_imported_energy"]["value"] == 8.0
    assert out["daily_imported_energy"]["source"] == "modbus_derived"
    # The device's own figure rides along so the two can be compared from the UI.
    assert out["daily_imported_energy"]["raw_register_value"] == 3.5
    assert state.baselines["total_imported_energy"].last_total == 6470.0


def test_grid_daily_omits_raw_attribute_when_the_register_was_absent():
    """Nothing to compare against → no ``raw_register_value`` attribute."""
    out, _, _ = apply_derived_daily_energy({"total_imported_energy": _point(6470.0)}, local_date=_DAY, state=_seeded())

    assert "raw_register_value" not in out["daily_imported_energy"]


def test_grid_daily_first_sample_seed_ignored_when_the_day_is_the_whole_counter():
    """``raw == total`` implies a start-of-day baseline of 0, which must not be trusted.

    A 0 baseline is indistinguishable from a glitched one and would hand back the whole
    lifetime total as "today" (#400), so the day reads 0 instead.
    """
    data = {"total_imported_energy": _point(12.4), "daily_imported_energy": _point(12.4)}

    _, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 0.0}
    assert state.baselines["total_imported_energy"].baseline == 12.4


def test_grid_daily_seeds_when_the_stored_entry_carries_no_history():
    """A partial/corrupt store entry is treated as no history, so it still seeds."""
    state = DerivedDailyEnergyState(baselines={"total_imported_energy": DerivedDailyBaseline()})
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(12.4)}

    _, new_state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=state)

    assert derived == {"daily_imported_energy": 12.4}
    assert round(new_state.baselines["total_imported_energy"].baseline, 3) == 6457.6


def test_grid_daily_first_sample_is_seeded_from_the_live_register():
    """Taking over mid-day must not throw away the part of the day already counted.

    Without a seed the baseline anchors at the current total, so the day we switch would
    read 0 until the next midnight. The device's own figure tells us where midnight was.
    """
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(12.4)}

    out, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 12.4}
    # 6470 − 12.4: the implied start-of-day total.
    assert round(state.baselines["total_imported_energy"].baseline, 3) == 6457.6
    assert out["daily_imported_energy"]["value"] == 12.4


def test_grid_daily_first_sample_seed_is_ignored_when_implausible():
    """A reading above the lifetime counter tells us nothing; fall back to a 0 day."""
    data = {"total_imported_energy": _point(100.0), "daily_imported_energy": _point(500.0)}

    _, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 0.0}
    assert state.baselines["total_imported_energy"].baseline == 100.0


def test_grid_daily_first_sample_flat_zero_register_starts_the_day_at_zero():
    """A flat-0 register carries no start-of-day information (#401), so the day starts at 0."""
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(0.0)}

    _, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=DerivedDailyEnergyState())

    assert derived == {"daily_imported_energy": 0.0}
    assert state.baselines["total_imported_energy"].baseline == 6470.0


def test_grid_daily_seed_is_not_reused_once_there_is_history():
    """Only a genuinely fresh start seeds from the register; later days anchor on yesterday."""
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DerivedDailyBaseline(
                baseline=6400.0, baseline_date=date(2026, 9, 19), last_total=6462.0
            )
        }
    )
    data = {"total_imported_energy": _point(6470.0), "daily_imported_energy": _point(99.0)}

    _, new_state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=state)

    # Anchored on yesterday's last total (6462), not on the bogus 99 the register reports.
    assert derived == {"daily_imported_energy": 8.0}
    assert new_state.baselines["total_imported_energy"].baseline == 6462.0


def test_grid_daily_silent_without_lifetime_total():
    """A meterless plant publishes nothing rather than a fabricated 0 (#387 contract)."""
    out, state, derived = apply_derived_daily_energy({}, local_date=_DAY, state=DerivedDailyEnergyState())

    assert out == {}
    assert derived == {}
    assert state.baselines == {}


def test_grid_daily_midnight_rollover_anchors_on_yesterdays_last_total():
    """A new local day starts from where the counter was at the previous day's end."""
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DerivedDailyBaseline(
                baseline=6400.0, baseline_date=date(2026, 9, 19), last_total=6462.0
            )
        }
    )

    _, new_state, derived = apply_derived_daily_energy(
        {"total_imported_energy": _point(6465.0)}, local_date=_DAY, state=state
    )

    assert derived["daily_imported_energy"] == 3.0
    assert new_state.baselines["total_imported_energy"].baseline == 6462.0
    assert new_state.baselines["total_imported_energy"].baseline_date == _DAY


def test_grid_daily_counters_track_independent_baselines():
    """Import and export cross midnight independently, so each keeps its own baseline."""
    data = {"total_imported_energy": _point(6470.0), "total_exported_energy": _point(2005.0)}

    out, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=_seeded())

    assert derived == {"daily_imported_energy": 8.0, "daily_exported_energy": 0.0}
    assert out["daily_imported_energy"]["value"] == 8.0
    # Export had no seeded baseline, so today starts at the current lifetime total.
    assert out["daily_exported_energy"]["value"] == 0.0
    assert set(state.baselines) == {"total_imported_energy", "total_exported_energy"}


def test_implausible_jump_rejects_a_counter_that_went_to_garbage():
    """A step no grid connection could supply is rejected (mkaiser#692 class)."""
    # 0.5 kWh in 30 s is 60 kW — busy, but physically possible.
    assert implausible_counter_jump(100.0, 100.5, 30.0) is False
    # The sentinel-ish garbage a disconnected meter produces is not.
    assert implausible_counter_jump(100.0, 4_294_967.0, 30.0) is True


def test_implausible_jump_allows_a_real_catch_up_after_a_gap():
    """A long gap raises the allowance, so a genuine catch-up is never rejected."""
    # HA was down for two days; a plant can legitimately have imported a few hundred kWh.
    assert implausible_counter_jump(100.0, 400.0, 2 * 24 * 3600) is False


@pytest.mark.parametrize(("previous", "elapsed"), [(None, 30.0), (100.0, None), (100.0, 0.0)])
def test_implausible_jump_fails_open_without_a_reference(previous, elapsed):
    """No previous sample, no elapsed time or a non-positive one → nothing is rejected."""
    assert implausible_counter_jump(previous, 999_999.0, elapsed) is False


def test_grid_daily_holds_an_untrusted_counter_without_moving_its_baseline():
    """An untrusted counter is skipped entirely: no derived value, no baseline movement.

    Leaving the baseline alone is what lets the day resume intact once the meter reads
    sanely again; publishing the device's own figure meanwhile avoids a dashboard spike.
    """
    data = {"total_imported_energy": _point(4_294_967.0), "daily_imported_energy": _point(1.5)}

    out, state, derived = apply_derived_daily_energy(
        data, local_date=_DAY, state=_seeded(), untrusted=frozenset({"total_imported_energy"})
    )

    assert derived == {}
    assert state.baselines["total_imported_energy"].last_total == 6462.0
    # The register's own reading is untouched rather than replaced.
    assert out["daily_imported_energy"] == _point(1.5)


def test_grid_daily_untrusted_counter_leaves_the_code_absent_when_there_is_no_register():
    """No register to fall back on → the entity reads unknown rather than a spike."""
    out, _, derived = apply_derived_daily_energy(
        {"total_imported_energy": _point(4_294_967.0)},
        local_date=_DAY,
        state=_seeded(),
        untrusted=frozenset({"total_imported_energy"}),
    )

    assert derived == {}
    assert "daily_imported_energy" not in out


def test_energy_daily_state_store_roundtrip():
    """Per-counter baselines survive serialize → deserialize."""
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DerivedDailyBaseline(baseline=6462.0, baseline_date=_DAY, last_total=6470.0),
            "total_exported_energy": DerivedDailyBaseline(baseline=100.0, baseline_date=_DAY, last_total=101.0),
        }
    )
    assert DerivedDailyEnergyState.from_store(state.to_store()) == state
    assert DerivedDailyEnergyState.from_store(None).baselines == {}
    assert DerivedDailyEnergyState.from_store({"total_imported_energy": "nope"}).baselines == {}


# ---------------------------------------------------------------------------
# Software-derived daily battery charge/discharge (#486)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total_code", "daily_code"),
    [("total_battery_charge", "daily_battery_charge"), ("total_battery_discharge", "daily_battery_discharge")],
)
def test_battery_daily_derived_from_the_lifetime_counter(total_code, daily_code):
    """The daily battery figure is today's share of the lifetime counter, not the raw register.

    Some SH firmware never resets 13039/13025 at midnight (#431), so the raw value can be a
    multi-day total; the derivation replaces it and keeps it alongside for comparison.
    """
    data = {total_code: _point(1250.4), daily_code: _point(87.3)}

    out, state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=_seeded(total_code, 1240.0))

    assert derived == {daily_code: 10.4}
    assert out[daily_code]["value"] == 10.4
    assert out[daily_code]["source"] == "modbus_derived"
    assert out[daily_code]["raw_register_value"] == 87.3
    assert state.baselines[total_code].last_total == 1250.4


def test_battery_daily_resets_at_local_midnight():
    """The first sample of a new day anchors on yesterday's last total, so the day restarts."""
    out, state, _ = apply_derived_daily_energy(
        {"total_battery_charge": _point(1251.0)},
        local_date=date(2026, 9, 21),
        state=DerivedDailyEnergyState(
            baselines={
                "total_battery_charge": DerivedDailyBaseline(baseline=1240.0, baseline_date=_DAY, last_total=1250.4)
            }
        ),
    )

    assert out["daily_battery_charge"]["value"] == 0.6
    assert state.baselines["total_battery_charge"].baseline == 1250.4


def test_battery_and_grid_counters_derive_side_by_side():
    """Battery pairs share the per-counter state without disturbing the grid baselines."""
    data = {"total_imported_energy": _point(6470.0), "total_battery_discharge": _point(900.0)}
    state = DerivedDailyEnergyState(
        baselines={
            "total_imported_energy": DerivedDailyBaseline(baseline=6462.0, baseline_date=_DAY, last_total=6462.0),
            "total_battery_discharge": DerivedDailyBaseline(baseline=895.5, baseline_date=_DAY, last_total=895.5),
        }
    )

    _, new_state, derived = apply_derived_daily_energy(data, local_date=_DAY, state=state)

    assert derived == {"daily_imported_energy": 8.0, "daily_battery_discharge": 4.5}
    assert set(new_state.baselines) == {"total_imported_energy", "total_battery_discharge"}


def test_each_counter_is_judged_against_its_own_ceiling():
    """Grid counters use the grid ceiling, battery counters the (lower) battery ceiling."""
    ceilings = {counter.total_code: counter.max_power_w for counter in DERIVED_DAILY_COUNTERS}
    assert ceilings == {
        "total_imported_energy": MAX_GRID_POWER_W,
        "total_exported_energy": MAX_GRID_POWER_W,
        "total_battery_charge": MAX_BATTERY_POWER_W,
        "total_battery_discharge": MAX_BATTERY_POWER_W,
    }
    # 0.6 kWh in 30 s is 72 kW: possible over a grid connection, not into a battery.
    assert implausible_counter_jump(100.0, 100.6, 30.0) is False
    assert implausible_counter_jump(100.0, 100.6, 30.0, max_power_w=MAX_BATTERY_POWER_W) is True
