"""Tests for the daily-repeating forced-charge / forced-discharge scheduler (#359)."""

from __future__ import annotations

import contextlib
from datetime import date, datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.sungrow.const import CONF_SCHEDULE_WINDOWS, DOMAIN
from custom_components.sungrow.schedule import ScheduleWindow, SungrowScheduler

# 2026-09-21 is a Monday, so ``weekday() == 0`` — the reference day for mask tests.
_MONDAY = date(2026, 9, 21)
_TUESDAY = date(2026, 9, 22)
_WEDNESDAY = date(2026, 9, 23)
_SUNDAY = date(2026, 9, 27)


def _at(now: time, on: date = _MONDAY) -> datetime:
    """Combine a wall-clock time with a date (Monday by default)."""
    return datetime.combine(on, now)


# ---------------------------------------------------------------------------
# ScheduleWindow — invariants + membership
# ---------------------------------------------------------------------------


def test_schedule_window_rejects_unknown_mode():
    """Unknown mode keys are refused at construction — the engine only drives
    ``force_charge`` / ``force_discharge``; ``self_consumption`` is used implicitly
    after a window ends but is not a valid window mode."""
    with pytest.raises(ValueError, match="Invalid schedule mode"):
        ScheduleWindow(start=time(1), end=time(5), mode="stop")
    with pytest.raises(ValueError, match="Invalid schedule mode"):
        ScheduleWindow(start=time(1), end=time(5), mode="self_consumption")


def test_schedule_window_rejects_zero_length():
    """A start == end window has zero length; setting it would be a user error."""
    with pytest.raises(ValueError, match="zero-length"):
        ScheduleWindow(start=time(1), end=time(1), mode="force_charge")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (time(0, 59), False),  # before start
        (time(1, 0), True),  # exactly start
        (time(3, 0), True),  # inside
        (time(4, 59), True),  # last minute inside
        (time(5, 0), False),  # exactly end (exclusive)
        (time(23, 59), False),  # after end
    ],
)
def test_schedule_window_contains_same_day(now, expected):
    """A ``start < end`` window covers ``[start, end)`` on the same day."""
    window = ScheduleWindow(start=time(1), end=time(5), mode="force_charge")
    assert window.contains(_at(now)) is expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (time(22, 59), False),  # before start
        (time(23, 0), True),  # exactly start
        (time(23, 59), True),  # inside, before midnight
        (time(0, 0), True),  # inside, after midnight
        (time(5, 59), True),  # inside, before end
        (time(6, 0), False),  # exactly end (exclusive)
        (time(12, 0), False),  # daytime — well outside
    ],
)
def test_schedule_window_contains_wrap_over_midnight(now, expected):
    """A ``start >= end`` window wraps over midnight: ``[start, 24:00) ∪ [00:00, end)``."""
    window = ScheduleWindow(start=time(23), end=time(6), mode="force_charge")
    assert window.contains(_at(now)) is expected


# ---------------------------------------------------------------------------
# ScheduleWindow — weekday mask (#433)
# ---------------------------------------------------------------------------


def test_day_mask_restricts_a_same_day_window():
    """A masked window only runs on its own weekdays."""
    window = ScheduleWindow(start=time(1), end=time(5), mode="force_charge", days=frozenset({0}))

    assert window.contains(_at(time(3), _MONDAY)) is True
    assert window.contains(_at(time(3), _TUESDAY)) is False
    assert window.contains(_at(time(3), _WEDNESDAY)) is False


def test_day_mask_attributes_a_wrapping_window_to_its_start_day():
    """Monday 23:30→06:00 runs into Tuesday morning; Tuesday 02:00 is Monday's window.

    This is the case a naive "is today's weekday in the mask" check gets wrong: it would
    either skip the second half of the window or run the window on Wednesday morning.
    """
    window = ScheduleWindow(start=time(23, 30), end=time(6), mode="force_charge", days=frozenset({0}))

    assert window.contains(_at(time(23, 45), _MONDAY)) is True  # Monday evening leg
    assert window.contains(_at(time(2), _TUESDAY)) is True  # spill from Monday
    assert window.contains(_at(time(2), _WEDNESDAY)) is False  # Tuesday night did not run
    assert window.contains(_at(time(23, 45), _TUESDAY)) is False  # mask is Monday-only


def test_day_mask_none_means_every_day():
    """No mask (the historical shape) runs every day, including wrapping windows."""
    window = ScheduleWindow(start=time(23, 30), end=time(6), mode="force_charge")

    assert window.contains(_at(time(3), _MONDAY)) is True
    assert window.contains(_at(time(3), _SUNDAY)) is True
    assert window.contains(_at(time(23, 45), _SUNDAY)) is True
    assert _SUNDAY.weekday() == 6


# ---------------------------------------------------------------------------
# SungrowScheduler.active_window — overlap resolution
# ---------------------------------------------------------------------------


def _entry_with_windows(hass: HomeAssistant, windows: list[dict]) -> MagicMock:
    """Build a MagicMock config entry with the given schedule windows."""
    entry = MagicMock()
    entry.title = "Test Entry"
    entry.entry_id = "test_entry_id"
    entry.options = {CONF_SCHEDULE_WINDOWS: windows}
    return entry


def test_active_window_returns_none_when_no_windows(hass: HomeAssistant):
    """No configured windows → nothing to activate."""
    scheduler = SungrowScheduler.from_entry(hass, _entry_with_windows(hass, []))
    assert scheduler.active_window(_at(time(3, 0))) is None


def test_active_window_returns_none_when_outside_every_window(hass: HomeAssistant):
    """A time outside every configured window resolves to ``None``."""
    scheduler = SungrowScheduler.from_entry(
        hass,
        _entry_with_windows(
            hass,
            [
                {"start": "01:00", "end": "05:00", "mode": "force_charge"},
                {"start": "17:00", "end": "20:00", "mode": "force_discharge"},
            ],
        ),
    )
    assert scheduler.active_window(_at(time(12, 0))) is None


def test_active_window_picks_latest_start_on_overlap(hass: HomeAssistant):
    """Overlapping windows: the one with the later start wins during the overlap.

    A short "force_discharge" window nested inside a longer "force_charge" one
    should override the enclosing charge during its span — otherwise a user
    couldn't cut a discharge slot into a broader charge session.
    """
    scheduler = SungrowScheduler.from_entry(
        hass,
        _entry_with_windows(
            hass,
            [
                {"start": "01:00", "end": "06:00", "mode": "force_charge"},
                {"start": "03:00", "end": "04:00", "mode": "force_discharge"},
            ],
        ),
    )
    # Inside overlap → shorter (later-started) window wins.
    active = scheduler.active_window(_at(time(3, 30)))
    assert active is not None
    assert active.mode == "force_discharge"
    # Outside the shorter window but still inside the longer one → longer wins.
    active = scheduler.active_window(_at(time(5, 0)))
    assert active is not None
    assert active.mode == "force_charge"


# ---------------------------------------------------------------------------
# SungrowScheduler.from_entry — malformed row tolerance
# ---------------------------------------------------------------------------


def test_from_entry_drops_malformed_rows_and_keeps_valid_ones(hass: HomeAssistant, caplog):
    """One typo shouldn't take the whole entry offline — bad rows are logged and
    skipped, valid ones proceed."""
    entry = _entry_with_windows(
        hass,
        [
            {"start": "01:00", "end": "05:00", "mode": "force_charge"},  # valid
            {"start": "not-a-time", "end": "10:00", "mode": "force_charge"},  # bad time
            {"start": "12:00", "end": "12:00", "mode": "force_charge"},  # zero length
            {"start": "20:00", "end": "22:00", "mode": "invalid_mode"},  # bad mode
            "not-a-dict",  # wrong shape entirely
            {"start": "23:00", "end": "06:00", "mode": "force_discharge"},  # valid wrap
        ],
    )
    scheduler = SungrowScheduler.from_entry(hass, entry)
    assert len(scheduler.windows) == 2
    assert scheduler.windows[0].mode == "force_charge"
    assert scheduler.windows[1].mode == "force_discharge"


# ---------------------------------------------------------------------------
# SungrowScheduler lifecycle
# ---------------------------------------------------------------------------


async def test_scheduler_start_stop_is_idempotent(hass: HomeAssistant):
    """``async_start`` twice replaces the callbacks cleanly; ``async_stop`` twice is safe."""
    entry = _entry_with_windows(hass, [{"start": "01:00", "end": "05:00", "mode": "force_charge"}])
    scheduler = SungrowScheduler.from_entry(hass, entry)

    with patch("custom_components.sungrow.schedule.async_track_time_change") as tracker:
        tracker.return_value = MagicMock()  # cancel callable
        await scheduler.async_start()
        # 2 callbacks per window: start + end.
        assert tracker.call_count == 2
        await scheduler.async_start()  # idempotent — replaces previous armings
        assert tracker.call_count == 4

    scheduler.async_stop()
    scheduler.async_stop()  # safe to call twice


async def test_scheduler_start_applies_active_window_on_setup(hass: HomeAssistant):
    """HA restart mid-window: setup issues the matching mode up front so the
    inverter doesn't drift outside the intended mode while the integration was down."""
    entry = _entry_with_windows(hass, [{"start": "01:00", "end": "05:00", "mode": "force_charge"}])
    scheduler = SungrowScheduler.from_entry(hass, entry)

    fake_select = MagicMock()
    fake_select.async_select_option = AsyncMock()
    fake_select.hass = hass
    fake_select.platform = None  # skip async_write_ha_state
    fake_select.registry_entry = None  # skip cross-entry filtering — this is our select

    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {"select.plant_battery": fake_select}

    with (
        patch("custom_components.sungrow.schedule.async_track_time_change") as tracker,
        patch("homeassistant.util.dt.now") as fake_now,
    ):
        tracker.return_value = MagicMock()
        # Simulate "now" being inside the 01:00-05:00 window.
        fake_now.return_value = _at(time(3, 0))
        await scheduler.async_start()

    # The select got its mode set to Force charge on setup.
    fake_select.async_select_option.assert_awaited_with("Force charge")
    scheduler.async_stop()


async def test_scheduler_no_active_window_on_setup_does_not_touch_selects(hass: HomeAssistant):
    """Outside every window at setup → no select is written to — the user's
    manual mode is preserved."""
    entry = _entry_with_windows(hass, [{"start": "01:00", "end": "05:00", "mode": "force_charge"}])
    scheduler = SungrowScheduler.from_entry(hass, entry)

    fake_select = MagicMock()
    fake_select.async_select_option = AsyncMock()
    fake_select.hass = hass
    fake_select.platform = None
    fake_select.registry_entry = None

    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {"select.plant_battery": fake_select}

    with (
        patch("custom_components.sungrow.schedule.async_track_time_change") as tracker,
        patch("homeassistant.util.dt.now") as fake_now,
    ):
        tracker.return_value = MagicMock()
        # "Now" is well outside every window.
        fake_now.return_value = _at(time(12, 0))
        await scheduler.async_start()

    fake_select.async_select_option.assert_not_awaited()
    scheduler.async_stop()


async def test_scheduler_skips_selects_owned_by_other_entries(hass: HomeAssistant):
    """A scheduler must only touch battery-mode selects owned by its own entry —
    another user's plant on the same HA instance shouldn't get scheduled by us."""
    entry = _entry_with_windows(hass, [{"start": "01:00", "end": "05:00", "mode": "force_charge"}])
    entry.entry_id = "entry_a"
    scheduler = SungrowScheduler.from_entry(hass, entry)

    # A select owned by *another* entry — its ``registry_entry.config_entry_id`` is not ours.
    other_select = MagicMock()
    other_select.async_select_option = AsyncMock()
    other_select.hass = hass
    other_select.platform = None
    other_select.registry_entry.config_entry_id = "entry_b"

    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {"select.other_plant": other_select}

    with (
        patch("custom_components.sungrow.schedule.async_track_time_change") as tracker,
        patch("homeassistant.util.dt.now") as fake_now,
    ):
        tracker.return_value = MagicMock()
        fake_now.return_value = _at(time(3, 0))
        await scheduler.async_start()

    other_select.async_select_option.assert_not_awaited()
    scheduler.async_stop()


async def test_scheduler_restores_enclosing_mode_when_inner_window_ends(hass: HomeAssistant):
    """When a nested window ends, the enclosing window's mode must be re-applied.

    Overlap policy is "latest start wins", so an inner window overrides the enclosing
    one only for its own span. Once the inner window ends, the outer window's mode has
    to be put back — otherwise the inverter keeps the inner mode until the outer window
    also ends (potentially hours of the wrong actuation).
    """
    entry = _entry_with_windows(
        hass,
        [
            {"start": "01:00", "end": "06:00", "mode": "force_charge"},
            {"start": "03:00", "end": "04:00", "mode": "force_discharge"},
        ],
    )
    scheduler = SungrowScheduler.from_entry(hass, entry)
    outer = next(w for w in scheduler.windows if w.start == time(1, 0))
    inner = next(w for w in scheduler.windows if w.start == time(3, 0))

    fake_select = MagicMock()
    fake_select.async_select_option = AsyncMock()
    fake_select.hass = hass
    fake_select.platform = None  # skip async_write_ha_state
    fake_select.registry_entry = None
    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {"select.plant_battery": fake_select}

    with patch("homeassistant.util.dt.now") as fake_now:
        # Inner window just ended; the enclosing 01:00-06:00 window is still active.
        fake_now.return_value = _at(time(4, 0))
        await scheduler._on_boundary_impl(inner, entering=False)

    fake_select.async_select_option.assert_awaited_with("Force charge")
    assert scheduler._current_window is outer


def _register_fake_select(hass: HomeAssistant) -> MagicMock:
    """Register a battery-mode select the scheduler can drive, and return it."""
    fake_select = MagicMock()
    fake_select.async_select_option = AsyncMock()
    fake_select.hass = hass
    fake_select.platform = None  # skip async_write_ha_state
    fake_select.registry_entry = None
    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {"select.plant_battery": fake_select}
    return fake_select


@pytest.mark.parametrize("entering", [True, False])
async def test_day_mask_boundary_is_inert_on_a_non_matching_weekday(hass: HomeAssistant, entering):
    """A masked window's boundaries fire every day, so a non-matching day must do nothing (#433).

    ``async_track_time_change`` arms one callback per boundary per *day*; there is no way to
    arm it for "Mondays only". Without the containment re-check a Monday-only window would
    actuate every Tuesday and hold that mode until its next boundary.
    """
    entry = _entry_with_windows(
        hass,
        [{"start": "01:00", "end": "05:00", "mode": "force_charge", "days": ["mon"]}],
    )
    scheduler = SungrowScheduler.from_entry(hass, entry)
    window = scheduler.windows[0]
    fake_select = _register_fake_select(hass)

    with patch("homeassistant.util.dt.now") as fake_now:
        fake_now.return_value = _at(time(1, 0), _TUESDAY)
        await scheduler._on_boundary_impl(window, entering=entering)

    fake_select.async_select_option.assert_not_awaited()
    assert scheduler._current_window is None


async def test_day_mask_boundary_actuates_on_a_matching_weekday(hass: HomeAssistant):
    """The same boundary on the window's own weekday still actuates normally."""
    entry = _entry_with_windows(
        hass,
        [{"start": "01:00", "end": "05:00", "mode": "force_charge", "days": ["mon"]}],
    )
    scheduler = SungrowScheduler.from_entry(hass, entry)
    window = scheduler.windows[0]
    fake_select = _register_fake_select(hass)

    with patch("homeassistant.util.dt.now") as fake_now:
        fake_now.return_value = _at(time(1, 0), _MONDAY)
        await scheduler._on_boundary_impl(window, entering=True)

    fake_select.async_select_option.assert_awaited_with("Force charge")
    assert scheduler._current_window is window


# ---------------------------------------------------------------------------
# Weekday-mask parsing (#433)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["mon"], {0}),
        (["mon", "fri"], {0, 4}),
        (["monday", "Friday"], {0, 4}),
        ([0, 4], {0, 4}),
        ("mon,fri", {0, 4}),
        ("sat sun", {5, 6}),
        # Every day, an empty list and an absent mask all mean "no mask".
        (None, None),
        ([], None),
        (["mon", "tue", "wed", "thu", "fri", "sat", "sun"], None),
    ],
)
def test_parse_days_accepts_the_shapes_a_row_can_hold(raw, expected):
    """The engine reads whatever the form or hand-authored options produce."""
    from custom_components.sungrow.schedule import _parse_days

    parsed = _parse_days(raw)
    assert parsed == (None if expected is None else frozenset(expected))


def test_parse_days_rejects_an_unknown_weekday():
    """A typo drops the row (with a warning) rather than silently running every day."""
    from custom_components.sungrow.schedule import _parse_days

    with pytest.raises(ValueError, match="Unknown weekday"):
        _parse_days(["monday", "funday"])


def test_window_rejects_an_empty_or_out_of_range_mask():
    """A mask that can never match, or names a day that doesn't exist, is a bug not a no-op."""
    with pytest.raises(ValueError, match="empty day mask"):
        ScheduleWindow(start=time(1), end=time(5), mode="force_charge", days=frozenset())
    with pytest.raises(ValueError, match="Invalid weekday numbers"):
        ScheduleWindow(start=time(1), end=time(5), mode="force_charge", days=frozenset({7}))


async def test_scheduler_stop_cancels_inflight_boundary_task(hass: HomeAssistant):
    """``async_stop`` cancels a boundary task that is still running.

    An entry reload tears the scheduler down while a mode change may be mid-write; the
    task must not keep actuating against the torn-down entry.
    """
    import asyncio

    entry = _entry_with_windows(hass, [{"start": "01:00", "end": "05:00", "mode": "force_charge"}])
    scheduler = SungrowScheduler.from_entry(hass, entry)

    started = asyncio.Event()

    async def _slow_boundary(window, *, entering):
        started.set()
        await asyncio.sleep(3600)

    with (
        patch("custom_components.sungrow.schedule.async_track_time_change") as tracker,
        patch.object(scheduler, "_on_boundary_impl", side_effect=_slow_boundary),
    ):
        tracker.return_value = MagicMock()
        await scheduler.async_start()
        # Fire one boundary callback synchronously (as async_track_time_change would).
        scheduler._make_transition_callback(scheduler.windows[0], entering=True)(None)

    await started.wait()
    assert scheduler._tasks  # the task is tracked

    task = next(iter(scheduler._tasks))
    scheduler.async_stop()

    assert not scheduler._tasks
    with contextlib.suppress(asyncio.CancelledError):
        await task
