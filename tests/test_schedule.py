"""Tests for the daily-repeating forced-charge / forced-discharge scheduler (#359)."""

from __future__ import annotations

from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.sungrow.const import CONF_SCHEDULE_WINDOWS, DOMAIN
from custom_components.sungrow.schedule import ScheduleWindow, SungrowScheduler

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
    assert window.contains(now) is expected


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
    assert window.contains(now) is expected


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
    assert scheduler.active_window(time(3, 0)) is None


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
    assert scheduler.active_window(time(12, 0)) is None


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
    active = scheduler.active_window(time(3, 30))
    assert active is not None
    assert active.mode == "force_discharge"
    # Outside the shorter window but still inside the longer one → longer wins.
    active = scheduler.active_window(time(5, 0))
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
        fake_now.return_value.time.return_value = time(3, 0)
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
        fake_now.return_value.time.return_value = time(12, 0)
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
        fake_now.return_value.time.return_value = time(3, 0)
        await scheduler.async_start()

    other_select.async_select_option.assert_not_awaited()
    scheduler.async_stop()
