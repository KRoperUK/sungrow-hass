"""Tests for sunrise/sunset-relative schedule boundaries (#482).

Sun times come from Home Assistant's own astral helper at fixed locations, so the
expected clock times below are literal. The London figures (51.5 N, 0.12 W, elevation
0) match published almanac times to the minute — e.g. 21 June 2026 sunrise 04:43 BST,
sunset 21:21 BST — which keeps the tests honest about which *day's* event is used.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.sungrow.const import CONF_SCHEDULE_WINDOWS, DOMAIN
from custom_components.sungrow.schedule import (
    ScheduleWindow,
    SunBoundary,
    SungrowScheduler,
    format_boundary,
    parse_boundary,
)

LONDON = ZoneInfo("Europe/London")
OSLO = ZoneInfo("Europe/Oslo")


async def _set_location(hass: HomeAssistant, lat: float, lon: float, tz: str) -> None:
    hass.config.latitude = lat
    hass.config.longitude = lon
    hass.config.elevation = 0
    await hass.config.async_set_time_zone(tz)


@pytest.fixture
async def london(hass: HomeAssistant) -> HomeAssistant:
    """Home Assistant located in London."""
    await _set_location(hass, 51.5, -0.12, "Europe/London")
    return hass


@pytest.fixture
async def tromso(hass: HomeAssistant) -> HomeAssistant:
    """Home Assistant located in Tromsø, inside the Arctic Circle (polar night and midnight sun)."""
    await _set_location(hass, 69.65, 18.96, "Europe/Oslo")
    return hass


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0, tz: ZoneInfo = LONDON) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def _entry(windows: list[dict]) -> MagicMock:
    entry = MagicMock()
    entry.title = "Test Entry"
    entry.entry_id = "test_entry_id"
    entry.options = {CONF_SCHEDULE_WINDOWS: windows}
    return entry


def _register_fake_select(hass: HomeAssistant) -> MagicMock:
    fake_select = MagicMock()
    fake_select.async_select_option = AsyncMock()
    fake_select.hass = hass
    fake_select.platform = None  # skip async_write_ha_state
    fake_select.registry_entry = None
    hass.data.setdefault(DOMAIN, {})["battery_mode_selects"] = {"select.plant_battery": fake_select}
    return fake_select


def _modes(select: MagicMock) -> list[str]:
    return [call.args[0] for call in select.async_select_option.await_args_list]


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: datetime) -> None:
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


def _armed(scheduler: SungrowScheduler) -> list[tuple[bool, str]]:
    """The armed solar boundaries as (entering, local HH:MM on date) for readable asserts."""
    return sorted(
        (entering, instant.astimezone(LONDON).strftime("%Y-%m-%d %H:%M"))
        for (_index, entering, instant) in scheduler._solar_timers
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sunset", SunBoundary("sunset")),
        ("sunrise+01:00", SunBoundary("sunrise", timedelta(hours=1))),
        ("Sunset - 0:30", SunBoundary("sunset", timedelta(minutes=-30))),
        ("sunrise+00:00:30", SunBoundary("sunrise", timedelta(seconds=30))),
        ("sunset+12:00", SunBoundary("sunset", timedelta(hours=12))),
        ("23:30", time(23, 30)),
        ("05:00:00", time(5)),
        (time(6, 15), time(6, 15)),
        (SunBoundary("sunset", timedelta(hours=-1)), SunBoundary("sunset", timedelta(hours=-1))),
    ],
)
def test_parse_boundary_accepts_fixed_and_sun_relative(raw, expected):
    """Fixed times parse exactly as before; sun boundaries accept case/space variations."""
    assert parse_boundary(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["sunset+12:01", "sunrise-13:00", "sunset+1:60", "sunsetx", "sunset+", "sunset 01:00", "noon", "dusk-00:10"],
)
def test_parse_boundary_rejects_malformed_or_out_of_range(raw):
    """Unknown anchors, offsets beyond ±12:00 and garbled offsets drop the row."""
    with pytest.raises(ValueError):
        parse_boundary(raw)


def test_sun_boundary_rejects_an_unknown_event():
    """Only sunrise and sunset are supported anchors."""
    with pytest.raises(ValueError, match="Unknown sun event"):
        SunBoundary("noon")


@pytest.mark.parametrize(
    ("boundary", "text"),
    [
        (SunBoundary("sunset"), "sunset"),
        (SunBoundary("sunset", timedelta(minutes=-30)), "sunset-00:30"),
        (SunBoundary("sunrise", timedelta(hours=1, minutes=5, seconds=9)), "sunrise+01:05:09"),
        (time(23, 30), "23:30"),
        (time(5, 0, 15), "05:00:15"),
    ],
)
def test_format_boundary_round_trips(boundary, text):
    """The canonical text is what the options flow stores, and it parses back unchanged."""
    assert format_boundary(boundary) == text
    assert parse_boundary(text) == boundary


def test_from_entry_keeps_sun_rows_alongside_fixed_ones(hass: HomeAssistant):
    """A stored sun-relative row is a plain string, so no options migration is needed."""
    scheduler = SungrowScheduler.from_entry(
        hass,
        _entry(
            [
                {"start": "01:00", "end": "05:00", "mode": "force_charge"},
                {"start": "sunset-00:30", "end": "sunset+03:00", "mode": "force_discharge", "days": ["mon"]},
                {"start": "sunset+03:00", "end": "sunset-00:30", "mode": "force_discharge"},  # reversed
                {"start": "sunrise+13:00", "end": "sunset", "mode": "force_charge"},  # offset too big
            ]
        ),
    )
    assert [w.is_solar for w in scheduler.windows] == [False, True]
    assert scheduler.windows[1].days == frozenset({0})


# ---------------------------------------------------------------------------
# Static validation and window shape
# ---------------------------------------------------------------------------


def test_same_anchor_window_ending_before_it_starts_is_rejected():
    """Same anchor, end offset before start offset: wrong on every day, so caught at parse time."""
    with pytest.raises(ValueError, match="on every day"):
        ScheduleWindow(SunBoundary("sunset", timedelta(hours=1)), SunBoundary("sunset"), "force_charge")
    with pytest.raises(ValueError, match="zero-length"):
        ScheduleWindow(SunBoundary("sunrise"), SunBoundary("sunrise"), "force_charge")


@pytest.mark.parametrize(
    ("start", "end", "wraps"),
    [
        ("sunrise", "sunset", False),
        ("sunset", "sunrise", True),
        ("22:00", "sunrise", True),
        ("sunset-00:30", "sunset+03:00", False),  # crosses midnight in summer, but never wraps
        ("sunset+06:00", "06:00", True),  # nominal 24:00 → 06:00 the next day
        ("sunset", "23:00", False),
        ("sunrise+01:00", "09:00", False),
        ("23:30", "06:00", True),  # fixed windows keep their rule
        ("01:00", "05:00", False),
    ],
)
def test_window_shape_is_decided_from_configuration(start, end, wraps):
    """Whether the end belongs to the next day is a property of the window, not of the season."""
    window = ScheduleWindow(parse_boundary(start), parse_boundary(end), "force_charge")
    assert window.wraps is wraps


def test_solar_contains_requires_a_resolver():
    """A sun-relative window cannot be evaluated without a location."""
    window = ScheduleWindow(SunBoundary("sunrise"), SunBoundary("sunset"), "force_charge")
    with pytest.raises(ValueError, match="sun-event resolver"):
        window.contains(datetime(2026, 6, 21, 12, tzinfo=UTC))


# ---------------------------------------------------------------------------
# Resolution against real sun times
# ---------------------------------------------------------------------------


async def test_resolves_the_local_days_sun_events(london: HomeAssistant):
    """``sunrise+01:00 → sunset-01:00`` on midsummer resolves to that day's events."""
    scheduler = SungrowScheduler(hass=london, entry=_entry([]))
    window = ScheduleWindow(parse_boundary("sunrise+01:00"), parse_boundary("sunset-01:00"), "force_charge")

    span = window.occurrence(date(2026, 6, 21), scheduler.sun_event)

    assert span is not None
    assert span[0].strftime("%Y-%m-%d %H:%M") == "2026-06-21 05:43"
    assert span[1].strftime("%Y-%m-%d %H:%M") == "2026-06-21 20:21"


async def test_offset_crossing_midnight_stays_with_its_anchor_day(london: HomeAssistant):
    """``sunset-00:30 → sunset+03:00`` (the issue's example) runs past midnight in summer.

    It is Sunday's window even at 00:10 Monday, so a Sunday-only mask covers the small
    hours and Monday evening stays off.
    """
    scheduler = SungrowScheduler.from_entry(
        london,
        _entry([{"start": "sunset-00:30", "end": "sunset+03:00", "mode": "force_discharge", "days": ["sun"]}]),
    )
    window = scheduler.windows[0]
    span = window.occurrence(date(2026, 6, 21), scheduler.sun_event)  # Sunday

    assert span is not None
    assert span[0].strftime("%Y-%m-%d %H:%M") == "2026-06-21 20:51"
    assert span[1].strftime("%Y-%m-%d %H:%M") == "2026-06-22 00:21"
    assert scheduler.active_window(_local(2026, 6, 22, 0, 10)) is window  # Monday 00:10
    assert scheduler.active_window(_local(2026, 6, 22, 0, 25)) is None
    assert scheduler.active_window(_local(2026, 6, 22, 21, 0)) is None  # Monday evening: masked
    assert window.occurrence(date(2026, 6, 22), scheduler.sun_event) is None


async def test_overnight_window_ending_at_sunrise(london: HomeAssistant):
    """``22:00 → sunrise`` wraps, ending at the *next* morning's sunrise."""
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "22:00", "end": "sunrise", "mode": "force_charge"}])
    )
    window = scheduler.windows[0]

    span = window.occurrence(date(2026, 12, 20), scheduler.sun_event)

    assert span is not None
    assert span[1].strftime("%Y-%m-%d %H:%M") == "2026-12-21 08:04"
    assert scheduler.active_window(_local(2026, 12, 21, 7, 0)) is window
    assert scheduler.active_window(_local(2026, 12, 21, 8, 10)) is None


async def test_offset_is_elapsed_time_across_the_dst_change(london: HomeAssistant):
    """``sunset+10:00`` on 24 Oct 2026 lands after the clocks go back at 02:00 BST.

    Sunset is 17:48 BST (16:48 UTC). Ten elapsed hours is 02:48 UTC = 02:48 GMT; wall-clock
    arithmetic would have said 03:48 GMT — eleven real hours.
    """
    scheduler = SungrowScheduler(hass=london, entry=_entry([]))
    window = ScheduleWindow(parse_boundary("sunset+09:00"), parse_boundary("sunset+10:00"), "force_charge")

    span = window.occurrence(date(2026, 10, 24), scheduler.sun_event)

    assert span is not None
    assert span[1].isoformat(timespec="minutes") == "2026-10-25T02:48+00:00"
    assert span[1] - span[0] == timedelta(hours=1)


async def test_fixed_boundary_in_the_spring_forward_gap_moves_an_hour_later(london: HomeAssistant):
    """01:30 does not exist on 29 Mar 2026 in London; the boundary falls at 02:30 BST, never earlier."""
    scheduler = SungrowScheduler(hass=london, entry=_entry([]))
    window = ScheduleWindow(parse_boundary("01:30"), parse_boundary("sunrise"), "force_charge")

    span = window.occurrence(date(2026, 3, 29), scheduler.sun_event)

    assert span is not None
    assert span[0].isoformat(timespec="minutes") == "2026-03-29T02:30+01:00"
    assert span[1].isoformat(timespec="minutes") == "2026-03-29T06:43+01:00"


async def test_resolved_times_contradicting_the_shape_skip_that_day(london: HomeAssistant):
    """``sunset → 21:00`` runs in winter but is skipped on midsummer, when sunset is 21:21.

    The alternative — wrapping to 21:00 tomorrow — would be a 23½-hour forced window.
    """
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset", "end": "21:00", "mode": "force_discharge"}])
    )
    window = scheduler.windows[0]

    assert window.occurrence(date(2026, 12, 21), scheduler.sun_event) is not None
    reason = window.resolve(date(2026, 6, 21), scheduler.sun_event)
    assert reason == "on 2026-06-21 its end (21:00) is not after its start (21:21)"
    assert scheduler.active_window(_local(2026, 6, 21, 21, 30)) is None


async def test_polar_night_and_midnight_sun_skip_the_window(tromso: HomeAssistant):
    """No sunrise/sunset that day: the window does not run, even half-fixed ones."""
    scheduler = SungrowScheduler.from_entry(
        tromso,
        _entry(
            [
                {"start": "sunrise", "end": "sunset", "mode": "force_charge"},
                {"start": "10:00", "end": "sunset", "mode": "force_discharge"},
            ]
        ),
    )
    sun_window, mixed_window = scheduler.windows

    for day in (date(2026, 12, 21), date(2026, 6, 21)):
        assert sun_window.resolve(day, scheduler.sun_event) == (
            f"no sunrise or sunset on {day.isoformat()} at this location"
        )
        assert mixed_window.resolve(day, scheduler.sun_event) == f"no sunset on {day.isoformat()} at this location"
    assert scheduler.active_window(_local(2026, 12, 21, 12, tz=OSLO)) is None
    # Outside the polar period the same windows run normally.
    assert sun_window.occurrence(date(2026, 3, 21), scheduler.sun_event) is not None


# ---------------------------------------------------------------------------
# Engine: arming, firing, restart, re-arm
# ---------------------------------------------------------------------------


async def test_solar_window_actuates_at_resolved_times(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Start before the window; it enters at sunset-00:30 and releases at sunset+03:00."""
    freezer.move_to(_local(2026, 6, 21, 12))
    select = _register_fake_select(london)
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset-00:30", "end": "sunset+03:00", "mode": "force_discharge"}])
    )
    await scheduler.async_start()

    assert _modes(select) == []
    assert (True, "2026-06-21 20:51") in _armed(scheduler)
    assert (False, "2026-06-22 00:21") in _armed(scheduler)

    await _advance(london, freezer, _local(2026, 6, 21, 20, 50))
    assert _modes(select) == []
    await _advance(london, freezer, _local(2026, 6, 21, 20, 52))
    assert _modes(select) == ["Force discharge"]
    await _advance(london, freezer, _local(2026, 6, 22, 0, 20))
    assert _modes(select) == ["Force discharge"]
    await _advance(london, freezer, _local(2026, 6, 22, 0, 22))
    assert _modes(select) == ["Force discharge", "Self-consumption"]
    scheduler.async_stop()
    assert scheduler._solar_timers == {}


async def test_restart_mid_window_applies_the_mode_once(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """A restart inside today's occurrence applies its mode once and never re-enters it.

    The start instant is in the past, so it is not armed; only the end is. A second
    ``async_start`` (an entry reload) behaves the same, from the resolved times.
    """
    freezer.move_to(_local(2026, 6, 21, 22, 0))
    select = _register_fake_select(london)
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset-00:30", "end": "sunset+03:00", "mode": "force_discharge"}])
    )
    await scheduler.async_start()

    assert _modes(select) == ["Force discharge"]
    assert (True, "2026-06-21 20:51") not in _armed(scheduler)
    assert (False, "2026-06-22 00:21") in _armed(scheduler)

    await _advance(london, freezer, _local(2026, 6, 21, 23, 0))
    assert _modes(select) == ["Force discharge"]
    await _advance(london, freezer, _local(2026, 6, 22, 0, 22))
    assert _modes(select) == ["Force discharge", "Self-consumption"]
    scheduler.async_stop()


async def test_midnight_reconcile_arms_each_new_day(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Over three days the window fires at each day's own sunrise, armed by the midnight re-run."""
    freezer.move_to(_local(2026, 6, 20, 12))
    select = _register_fake_select(london)
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunrise+00:30", "end": "sunrise+02:00", "mode": "force_charge"}])
    )
    await scheduler.async_start()
    # Two days of look-ahead from Saturday noon: Sunday and Monday mornings.
    assert [t for e, t in _armed(scheduler) if e] == ["2026-06-21 05:13", "2026-06-22 05:13"]

    for day in (21, 22, 23):
        await _advance(london, freezer, _local(2026, 6, day, 0, 0))
        await _advance(london, freezer, _local(2026, 6, day, 5, 15))
        await _advance(london, freezer, _local(2026, 6, day, 6, 45))
    assert _modes(select) == ["Force charge", "Self-consumption"] * 3
    # Tuesday 23rd's occurrence was armed by a midnight run, not at start.
    assert scheduler._solar_timers
    scheduler.async_stop()


async def test_reconcile_is_idempotent_and_keeps_due_timers(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Re-running the reconcile (as midnight does) neither duplicates nor drops a timer."""
    freezer.move_to(_local(2026, 6, 21, 12))
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset", "end": "sunset+01:00", "mode": "force_charge"}])
    )
    await scheduler.async_start()
    before = dict(scheduler._solar_timers)

    scheduler._reconcile_solar(_local(2026, 6, 21, 12))

    assert scheduler._solar_timers == before
    scheduler.async_stop()


async def test_weekday_masked_solar_window_arms_nothing_on_other_days(
    london: HomeAssistant, freezer: FrozenDateTimeFactory
):
    """A Monday-only solar window arms Monday's boundaries and nothing for Sunday or Tuesday."""
    freezer.move_to(_local(2026, 6, 21, 0, 30))  # Sunday
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunrise", "end": "sunset", "mode": "force_charge", "days": ["mon"]}])
    )
    await scheduler.async_start()

    assert _armed(scheduler) == [(False, "2026-06-22 21:21"), (True, "2026-06-22 04:43")]
    scheduler.async_stop()


async def test_polar_day_arms_nothing_and_warns_once(
    tromso: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
):
    """Polar night: nothing is armed and each skipped day is reported once, not every reconcile."""
    freezer.move_to(_local(2026, 12, 21, 12, tz=OSLO))
    select = _register_fake_select(tromso)
    scheduler = SungrowScheduler.from_entry(
        tromso, _entry([{"start": "sunrise", "end": "sunset", "mode": "force_charge"}])
    )
    await scheduler.async_start()

    assert scheduler._solar_timers == {}
    assert _modes(select) == []
    warnings = [r for r in caplog.records if "no sunrise or sunset on 2026-12-21" in r.getMessage()]
    assert len(warnings) == 1
    scheduler._reconcile_solar(_local(2026, 12, 21, 13, tz=OSLO))
    assert len([r for r in caplog.records if "no sunrise or sunset on 2026-12-21" in r.getMessage()]) == 1
    scheduler.async_stop()


async def test_dst_night_boundary_fires_at_the_elapsed_instant(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Across the October change the end boundary is armed at 02:48 GMT, not 03:48."""
    freezer.move_to(_local(2026, 10, 24, 12))
    select = _register_fake_select(london)
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset+09:00", "end": "sunset+10:00", "mode": "force_charge"}])
    )
    await scheduler.async_start()
    assert (False, "2026-10-25 02:48") in _armed(scheduler)

    await _advance(london, freezer, datetime(2026, 10, 25, 1, 49, tzinfo=UTC))  # 01:49 GMT
    assert _modes(select) == ["Force charge"]
    await _advance(london, freezer, datetime(2026, 10, 25, 2, 49, tzinfo=UTC))  # 02:49 GMT
    assert _modes(select) == ["Force charge", "Self-consumption"]
    scheduler.async_stop()


async def test_location_change_rearms_solar_boundaries(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Moving the home re-resolves the sun times; stale timers are cancelled."""
    freezer.move_to(_local(2026, 6, 21, 12))
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset", "end": "sunset+01:00", "mode": "force_charge"}])
    )
    await scheduler.async_start()
    assert (True, "2026-06-21 21:21") in _armed(scheduler)

    london.config.latitude = 55.95  # Edinburgh: later midsummer sunset
    london.config.longitude = -3.19
    london.bus.async_fire(EVENT_CORE_CONFIG_UPDATE)
    await london.async_block_till_done()

    armed = _armed(scheduler)
    assert (True, "2026-06-21 21:21") not in armed
    assert any(e and t.startswith("2026-06-21 22:0") for e, t in armed)
    scheduler.async_stop()


async def test_solar_and_fixed_windows_overlap_by_latest_start(london: HomeAssistant):
    """The existing overlap policy holds: a solar window starting later overrides a fixed one."""
    scheduler = SungrowScheduler.from_entry(
        london,
        _entry(
            [
                {"start": "16:00", "end": "23:00", "mode": "force_charge"},
                {"start": "sunset-00:30", "end": "sunset+00:30", "mode": "force_discharge"},
            ]
        ),
    )
    active = scheduler.active_window(_local(2026, 6, 21, 21, 0))
    assert active is not None and active.mode == "force_discharge"
    active = scheduler.active_window(_local(2026, 6, 21, 22, 0))
    assert active is not None and active.mode == "force_charge"


async def test_solar_end_restores_enclosing_fixed_window(london: HomeAssistant):
    """When a nested solar window ends, the enclosing fixed window's mode is put back."""
    select = _register_fake_select(london)
    scheduler = SungrowScheduler.from_entry(
        london,
        _entry(
            [
                {"start": "16:00", "end": "23:00", "mode": "force_charge"},
                {"start": "sunset-00:30", "end": "sunset+00:30", "mode": "force_discharge"},
            ]
        ),
    )
    solar = scheduler.windows[1]
    span = solar.occurrence(date(2026, 6, 21), scheduler.sun_event)
    assert span is not None

    await scheduler._on_boundary_impl(solar, entering=False, at=span[1])

    assert _modes(select) == ["Force charge"]
    assert scheduler._current_window is scheduler.windows[0]


async def test_fixed_only_schedule_arms_no_solar_machinery(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """Backward compatibility: fixed windows keep two daily time-change callbacks and nothing else."""
    freezer.move_to(_local(2026, 6, 21, 12))
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "01:00", "end": "05:00", "mode": "force_charge"}])
    )
    await scheduler.async_start()

    assert len(scheduler._cancels) == 2
    assert scheduler._solar_timers == {}
    scheduler.async_stop()


async def test_stop_cancels_solar_timers_before_they_fire(london: HomeAssistant, freezer: FrozenDateTimeFactory):
    """After ``async_stop`` (entry unload) a due solar boundary does nothing."""
    freezer.move_to(_local(2026, 6, 21, 12))
    select = _register_fake_select(london)
    scheduler = SungrowScheduler.from_entry(
        london, _entry([{"start": "sunset", "end": "sunset+01:00", "mode": "force_charge"}])
    )
    await scheduler.async_start()
    scheduler.async_stop()

    await _advance(london, freezer, _local(2026, 6, 21, 21, 30))
    assert _modes(select) == []
