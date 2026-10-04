"""Daily-repeating forced-charge / forced-discharge schedule engine (#359).

Users on tariff plans want to charge cheap-overnight and (optionally) discharge
peak-daytime. Before #359 this required a HA automation calling
``sungrow.set_battery_mode`` at the boundaries with correct handling of
duration-based auto-revert, HA restarts mid-window, and automation-vs-UI
conflicts — turning every tariff user into an automation author.

This module owns a lightweight scheduler that:

* Reads a list of :class:`ScheduleWindow` from the entry options.
* Applies the correct battery mode at each window boundary via the same
  registered battery-mode select entities that :mod:`.services` uses.
* Handles HA restarts by evaluating the currently-active window at start and
  issuing the matching command up front, so the inverter doesn't drift out of
  the intended mode while the integration was down.
* Handles overlapping windows by picking the one with the latest start time —
  a superset window that fully contains a shorter one loses to the shorter one
  during its overlap.

Windows repeat daily, optionally restricted to a weekday mask (#433), and either
boundary may be a fixed local time or an offset from sunrise / sunset (#482). TOU
tariff plans are out of scope (they belong on the Energy dashboard or a separate
feature).

Fixed-time windows are armed once with ``async_track_time_change`` and repeat for
free. A sun-relative boundary moves every day, so *solar* windows (any window with
a sun-relative boundary) are instead resolved to concrete instants per day and armed
as one-shot ``async_track_point_in_time`` callbacks, re-reconciled at every local
midnight and whenever the Home Assistant location changes. See
:class:`SunBoundary` and :meth:`ScheduleWindow.occurrence` for the edge-case rules.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_change
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import CONF_SCHEDULE_WINDOWS, DOMAIN

if TYPE_CHECKING:
    from . import SungrowConfigEntry

_LOGGER = logging.getLogger(__name__)

# Accepted mode strings in the window dict — mirrors the keys accepted by
# ``sungrow.set_battery_mode`` so schedule authoring and the manual service use
# the same vocabulary.
_SCHEDULE_MODES: frozenset[str] = frozenset({"force_charge", "force_discharge"})

# The mode applied at the *end* of every window — release the battery back to
# the plant's Self-consumption behaviour so the user can resume manual control
# outside the scheduled window.
_MODE_AFTER_WINDOW = "self_consumption"

# Weekday names accepted in a window's ``days`` mask, mapped to ``datetime.weekday()``
# (Monday = 0). The three-letter forms are what the options-flow multi-select emits;
# the full names make hand-authored options and YAML just as readable.
_DAY_NAMES: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

# Shortest form of each weekday, Monday first — the values the options-flow multi-select
# uses and the order they are offered in.
SCHEDULE_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# Sun events a boundary can be anchored to (#482). Only the two the issue asks for:
# dawn/dusk/noon would need a solar-depression choice nobody has asked to make.
SUN_EVENTS: tuple[str, ...] = ("sunrise", "sunset")

# Largest offset accepted from a sun event. Twelve hours covers every realistic use
# (``sunset+06:00`` for an overnight charge) while keeping a boundary within a day and a
# half of its anchor, which bounds the days the engine has to scan when resolving.
MAX_SUN_OFFSET = timedelta(hours=12)

# Nominal clock times used for one thing only: deciding a solar window's *shape* —
# whether its end belongs to the day after its start (#482). ``22:00 → sunrise`` is an
# overnight window and ``sunrise → sunset`` a daytime one, whatever the season; the real
# sun times are used for everything else.
_NOMINAL_SUN_TIME: dict[str, timedelta] = {"sunrise": timedelta(hours=6), "sunset": timedelta(hours=18)}

_SUN_BOUNDARY_RE = re.compile(r"^(sunrise|sunset)\s*(?:([+-])\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?$")

# How far ahead solar boundaries are armed. Re-reconciled every local midnight, so two
# days of look-ahead means a skipped or late midnight run (DST, a busy event loop) can
# never leave a boundary unarmed.
_SOLAR_ARM_HORIZON = timedelta(hours=48)

type SunEventResolver = Callable[[date, str], datetime | None]


@dataclass(frozen=True)
class SunBoundary:
    """A window boundary at an offset from sunrise or sunset (#482).

    ``offset`` is *elapsed* time from the sun event, not wall-clock arithmetic, so
    ``sunset+03:00`` is three real hours after sunset even when a DST change falls in
    between. A boundary belongs to the day of the sun event it is anchored to, even when
    the offset carries it past midnight: Monday's ``sunset+03:00`` can be 00:20 Tuesday.
    """

    event: str
    offset: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        """Validate the event name and the offset bound."""
        if self.event not in SUN_EVENTS:
            raise ValueError(f"Unknown sun event {self.event!r}; expected one of {', '.join(SUN_EVENTS)}")
        if abs(self.offset) > MAX_SUN_OFFSET:
            raise ValueError(f"Sun offset {self} is larger than ±12:00")

    def __str__(self) -> str:
        """Render the canonical ``sunset-00:30`` form the options store."""
        if not self.offset:
            return self.event
        sign = "-" if self.offset < timedelta(0) else "+"
        total = int(abs(self.offset).total_seconds())
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        text = f"{self.event}{sign}{hours:02d}:{minutes:02d}"
        return f"{text}:{seconds:02d}" if seconds else text


type Boundary = time | SunBoundary


def format_boundary(boundary: Boundary) -> str:
    """Render a boundary for log lines and the stored options."""
    if isinstance(boundary, SunBoundary):
        return str(boundary)
    return boundary.strftime("%H:%M:%S" if boundary.second else "%H:%M")


def _nominal(boundary: Boundary) -> timedelta:
    """Return a boundary's nominal offset from midnight, used only to decide a window's shape."""
    if isinstance(boundary, SunBoundary):
        return _NOMINAL_SUN_TIME[boundary.event] + boundary.offset
    return timedelta(hours=boundary.hour, minutes=boundary.minute, seconds=boundary.second)


def _resolve_boundary(boundary: Boundary, day: date, sun: SunEventResolver) -> datetime | None:
    """Resolve ``boundary`` on ``day`` to an aware local datetime, or None if it has no time that day.

    A sun-relative boundary is None on a polar day or night, when its event does not
    happen. A fixed time that falls in a spring-forward gap resolves to the instant an
    hour later (the wall-clock reading moved past it), and an ambiguous fall-back time to
    its first occurrence — Python's ``fold=0`` rule, applied consistently.
    """
    if isinstance(boundary, SunBoundary):
        event = sun(day, boundary.event)
        if event is None:
            return None
        # Add the offset in UTC so it is elapsed time, not wall-clock time, across DST.
        return dt_util.as_local(dt_util.as_utc(event) + boundary.offset)
    wall = datetime.combine(day, boundary.replace(tzinfo=None), tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(dt_util.as_utc(wall))


@dataclass(frozen=True)
class ScheduleWindow:
    """A single repeating window that arms one battery mode.

    ``start`` and ``end`` are each a local wall-clock :class:`~datetime.time` or a
    :class:`SunBoundary`. For a fixed-time window, ``start >= end`` wraps over midnight —
    ``23:30`` → ``06:00`` means "from 23:30 today until 06:00 tomorrow".

    A *solar* window (either boundary sun-relative) decides whether it wraps once, from
    its configuration, by placing sunrise at a nominal 06:00 and sunset at 18:00 (see
    :attr:`wraps`). Its real times are resolved per day in :meth:`occurrence`.

    ``days`` restricts the window to a set of weekdays (``datetime.weekday()``,
    Monday = 0). ``None`` means every day, which is what a window without a mask
    has always meant, so existing configurations are unaffected.
    """

    start: Boundary
    end: Boundary
    mode: str
    days: frozenset[int] | None = None

    def __post_init__(self) -> None:
        """Validate the mode, the boundaries and the optional weekday mask."""
        if self.mode not in _SCHEDULE_MODES:
            raise ValueError(f"Invalid schedule mode: {self.mode!r}")
        if self.start == self.end:
            raise ValueError(
                f"Window start ({format_boundary(self.start)}) equals end ({format_boundary(self.end)}); "
                "a zero-length window has no effect"
            )
        if (
            isinstance(self.start, SunBoundary)
            and isinstance(self.end, SunBoundary)
            and self.start.event == self.end.event
            and self.end.offset < self.start.offset
        ):
            # Same anchor, so the order is the same every day: statically never runs.
            raise ValueError(f"Window end ({self.end}) is before its start ({self.start}) on every day")
        if self.days is not None:
            if not self.days:
                raise ValueError("Window has an empty day mask; it would never run")
            invalid = sorted(day for day in self.days if day not in range(7))
            if invalid:
                raise ValueError(f"Invalid weekday numbers {invalid}; expected 0 (Monday) to 6 (Sunday)")

    @property
    def is_solar(self) -> bool:
        """Return True if either boundary is sun-relative, so its times move every day."""
        return isinstance(self.start, SunBoundary) or isinstance(self.end, SunBoundary)

    @property
    def wraps(self) -> bool:
        """Return True if the window's end belongs to the day after its start.

        Fixed windows wrap when ``start >= end``, as they always have. A solar window
        compares nominal times (sunrise 06:00, sunset 18:00, plus offsets, *not* folded into
        a day): ``22:00 → sunrise`` and ``sunset → sunrise`` wrap; ``sunrise → sunset``,
        ``sunset-00:30 → sunset+03:00`` and ``sunset+06:00 → 23:00`` do not. Deciding the
        shape statically, rather than "wrap whenever today's end is not after today's
        start", is what stops a ``sunset → 23:00`` window turning into a 23½-hour forced
        discharge on the days sunset falls after 23:00 — that day is skipped instead.
        """
        if not self.is_solar:
            return _nominal(self.end) <= _nominal(self.start)
        return _nominal(self.end) < _nominal(self.start)

    @property
    def label(self) -> str:
        """Human-readable span for log lines, e.g. ``23:30-06:00`` or ``sunset-00:30 to sunset+03:00``."""
        separator = " to " if self.is_solar else "-"
        return f"{format_boundary(self.start)}{separator}{format_boundary(self.end)}"

    def runs_on(self, weekday: int) -> bool:
        """Return True if this window runs on ``weekday`` (``datetime.weekday()``)."""
        return self.days is None or weekday in self.days

    def resolve(self, day: date, sun: SunEventResolver) -> tuple[datetime, datetime] | str:
        """Resolve the occurrence that belongs to ``day`` into ``(start, end)`` local datetimes.

        Returns a reason string instead when the window does not run that day. The empty
        string means "masked out by its weekdays" (expected, never logged); anything else
        explains a skip worth telling the user about:

        * a sun event that does not happen that day (polar day/night) — the window does not
          run that day and arms nothing; there is no fixed-time fallback to guess at;
        * resolved times that contradict the window's shape (end at or before start) —
          skipped for that day rather than stretched across midnight.
        """
        if not self.runs_on(day.weekday()):
            return ""
        start = _resolve_boundary(self.start, day, sun)
        end = _resolve_boundary(self.end, day + timedelta(days=1) if self.wraps else day, sun)
        if start is None or end is None:
            missing = [
                b.event
                for b, value in ((self.start, start), (self.end, end))
                if isinstance(b, SunBoundary) and value is None
            ]
            return f"no {' or '.join(dict.fromkeys(missing))} on {day.isoformat()} at this location"
        if end <= start:
            return (
                f"on {day.isoformat()} its end ({end.strftime('%H:%M')}) is not after its start "
                f"({start.strftime('%H:%M')})"
            )
        return start, end

    def occurrence(self, day: date, sun: SunEventResolver) -> tuple[datetime, datetime] | None:
        """Return the ``(start, end)`` occurrence belonging to ``day``, or None if it doesn't run."""
        resolved = self.resolve(day, sun)
        return None if isinstance(resolved, str) else resolved

    def occurrence_at(self, moment: datetime, sun: SunEventResolver) -> tuple[datetime, datetime] | None:
        """Return the resolved occurrence containing ``moment``, or None.

        An occurrence belongs to its anchor day but can start the evening before (a negative
        offset) and end up to two days later (a wrapping end with a positive offset), so the
        days around ``moment`` are all checked; the latest-starting match wins.
        """
        local = dt_util.as_local(moment)
        today = local.date()
        for delta in (1, 0, -1, -2):
            span = self.occurrence(today + timedelta(days=delta), sun)
            if span is not None and span[0] <= local < span[1]:
                return span
        return None

    def contains(self, moment: datetime, sun: SunEventResolver | None = None) -> bool:
        """Return True if ``moment`` (local) is inside this window.

        Takes a full ``datetime`` rather than a ``time`` because a weekday mask has to
        know which day the window belongs to, and a wrapping window's small-hours leg
        belongs to the day it *started* on: a Monday-only ``23:30 → 06:00`` runs into
        Tuesday morning, and ``06:00`` Tuesday is still "Monday's" window.

        A solar window needs ``sun`` to resolve its boundaries for the days around
        ``moment``; fixed windows keep the wall-clock comparison they have always used.
        """
        if self.is_solar:
            if sun is None:
                raise ValueError(f"Window {self.label} is sun-relative and needs a sun-event resolver")
            return self.occurrence_at(moment, sun) is not None
        start, end = self.start, self.end
        assert isinstance(start, time) and isinstance(end, time)  # narrowed by is_solar
        now = moment.time()
        if start < end:
            return self.runs_on(moment.weekday()) and start <= now < end
        # Wraps over midnight: either tonight's leg (from ``start``) or yesterday's
        # spill into this morning. ``end`` is exclusive, so the closing boundary is out.
        if now >= start:
            return self.runs_on(moment.weekday())
        return now < end and self.runs_on((moment.weekday() - 1) % 7)


@dataclass
class SungrowScheduler:
    """Per-entry schedule engine (#359).

    Instantiated in :func:`~custom_components.sungrow.async_setup_entry` for
    every cloud entry with at least one battery-capable plant. On
    :meth:`async_start` it evaluates the currently-active window and applies
    the matching mode up front, then arms future transitions: fixed-time windows
    get ``async_track_time_change`` callbacks that repeat daily, solar windows get
    one-shot callbacks for their resolved instants, reconciled every local midnight
    (#482). :meth:`async_stop` releases every callback so unloading / reloading the
    entry is clean.
    """

    hass: HomeAssistant
    entry: SungrowConfigEntry
    windows: list[ScheduleWindow] = field(default_factory=list)
    _cancels: list[CALLBACK_TYPE] = field(default_factory=list, init=False, repr=False)
    _current_window: ScheduleWindow | None = field(default=None, init=False, repr=False)
    # In-flight boundary tasks, so a reload/unload can cancel a mode change that is
    # mid-write instead of letting it actuate against a torn-down entry.
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    # One-shot solar boundary timers keyed by (window index, entering, instant), so a
    # reconcile only arms what is new and cancels what no longer applies (#482).
    _solar_timers: dict[tuple[int, bool, datetime], CALLBACK_TYPE] = field(default_factory=dict, init=False, repr=False)
    # Sun events per (local date, event): astral is cheap but every contains() check
    # resolves several days, and the answer for a date never changes at one location.
    _sun_cache: dict[tuple[date, str], datetime | None] = field(default_factory=dict, init=False, repr=False)
    # (window index, day) skips already logged, so a polar winter warns once per day.
    _reported_skips: set[tuple[int, date]] = field(default_factory=set, init=False, repr=False)

    @classmethod
    def from_entry(cls, hass: HomeAssistant, entry: SungrowConfigEntry) -> SungrowScheduler:
        """Build a scheduler for ``entry`` by parsing its options.

        Malformed windows are dropped with a warning rather than failing the
        whole entry setup — a typo in one row shouldn't take the integration
        offline. The user sees the warning and fixes the row via the options
        flow (which validates the same way).
        """
        raw = entry.options.get(CONF_SCHEDULE_WINDOWS) or []
        windows: list[ScheduleWindow] = []
        for i, row in enumerate(raw):
            try:
                windows.append(_parse_window(row))
            except (ValueError, TypeError, KeyError) as err:
                _LOGGER.warning(
                    "Dropping malformed schedule window #%d on entry %s: %s (row=%r)",
                    i,
                    entry.title,
                    err,
                    row,
                )
        return cls(hass=hass, entry=entry, windows=windows)

    def sun_event(self, day: date, event: str) -> datetime | None:
        """Return ``event`` (sunrise/sunset) on local ``day`` at the HA location, or None.

        Uses ``get_astral_event_date`` (astral against ``hass.config`` latitude/longitude)
        rather than the ``sun`` entity: the entity only exposes the *next* rising/setting,
        which after sunrise is already tomorrow's, and it may not be loaded at all. None
        means the event does not happen that day (polar day or night).
        """
        key = (day, event)
        if key not in self._sun_cache:
            self._sun_cache[key] = get_astral_event_date(self.hass, event, day)
        return self._sun_cache[key]

    def _contains(self, window: ScheduleWindow, moment: datetime) -> bool:
        return window.contains(moment, self.sun_event)

    def active_window(self, moment: datetime) -> ScheduleWindow | None:
        """Return the schedule window active at ``moment`` (local), or None.

        Windows whose weekday mask excludes ``moment``'s day are skipped.

        Overlap policy: multiple windows can match a given time; the one with
        the latest ``start`` wins. That's the intuitive "most recently entered
        wins" behaviour — a superset window fully containing a shorter one
        gets overridden during the shorter window's span. Wrap-over-midnight
        starts count as their raw ``start`` time (a 23:30-06:00 window's start
        is 23:30, later than a 08:00 window's start). A solar window's start is
        compared by the clock time it resolved to for the occurrence in play.
        """
        best: tuple[time, ScheduleWindow] | None = None
        for window in self.windows:
            if window.is_solar:
                span = window.occurrence_at(moment, self.sun_event)
                if span is None:
                    continue
                key = span[0].time()
            else:
                if not window.contains(moment):
                    continue
                assert isinstance(window.start, time)  # fixed window
                key = window.start.replace(tzinfo=None)
            if best is None or key > best[0]:
                best = (key, window)
        return None if best is None else best[1]

    async def async_start(self) -> None:
        """Apply the currently-active window's mode and arm boundary callbacks.

        Idempotent: safe to call twice — a second call is treated as a reload
        and drops any callbacks the previous call installed. That's what lets
        the options-flow's ``OptionsFlowWithReload`` reload the entry without
        leaking stale timers.

        Restarting mid-window applies that window's mode exactly once, here, from the
        resolved times; only boundaries strictly in the future are armed afterwards, so
        a window that already started today is never "entered" a second time.
        """
        self.async_stop()
        if not self.windows:
            _LOGGER.debug("Scheduler for entry %s has no windows; nothing to arm", self.entry.title)
            return

        # Apply the mode active *right now* so an HA restart mid-window doesn't
        # leave the inverter drifting outside the intended mode.
        now_local = dt_util.now()
        self._current_window = self.active_window(now_local)
        if self._current_window is not None:
            _LOGGER.info(
                "Entry %s: applying scheduled mode %s (window %s) on setup",
                self.entry.title,
                self._current_window.mode,
                self._current_window.label,
            )
            await self._apply_mode(self._current_window.mode)
        else:
            _LOGGER.debug("Entry %s: no schedule window active at %s", self.entry.title, now_local.time())

        # Arm one time-change callback per fixed window boundary. ``async_track_time_change``
        # fires every day at the specified HH:MM:00, so we get daily repetition for free
        # without having to reschedule after each fire.
        for i, window in enumerate(self.windows):
            if window.is_solar:
                continue
            assert isinstance(window.start, time) and isinstance(window.end, time)
            self._cancels.append(
                async_track_time_change(
                    self.hass,
                    self._make_transition_callback(window, entering=True),
                    hour=window.start.hour,
                    minute=window.start.minute,
                    second=0,
                )
            )
            self._cancels.append(
                async_track_time_change(
                    self.hass,
                    self._make_transition_callback(window, entering=False),
                    hour=window.end.hour,
                    minute=window.end.minute,
                    second=0,
                )
            )
            _LOGGER.debug("Entry %s: armed schedule window #%d (%s %s)", self.entry.title, i, window.mode, window.label)

        if any(window.is_solar for window in self.windows):
            # Solar boundaries move daily: arm the next two days' worth now, then top the
            # set up every local midnight and whenever the location/time zone changes.
            self._reconcile_solar(now_local)
            self._cancels.append(
                async_track_time_change(self.hass, self._on_local_midnight, hour=0, minute=0, second=0)
            )
            self._cancels.append(self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._on_core_config_update))

    @callback
    def async_stop(self) -> None:
        """Cancel every armed transition callback and in-flight task. Idempotent."""
        for cancel in self._cancels:
            cancel()
        self._cancels.clear()
        for cancel in self._solar_timers.values():
            cancel()
        self._solar_timers.clear()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._sun_cache.clear()
        self._reported_skips.clear()
        self._current_window = None

    @callback
    def _on_local_midnight(self, now: datetime) -> None:
        self._reconcile_solar(now)

    @callback
    def _on_core_config_update(self, _event: Event[Any]) -> None:
        # The location or time zone may have changed: every cached sun time is suspect.
        # Re-arming does not re-apply a mode; the next boundary does that.
        self._sun_cache.clear()
        self._reported_skips.clear()
        self._reconcile_solar(dt_util.now())

    @callback
    def _reconcile_solar(self, now: datetime) -> None:
        """Arm every solar boundary in ``(now, now + horizon]`` that is not armed yet (#482).

        Each window is resolved per anchor day; a window that does not run on a day (its
        weekday mask, a polar day/night, or resolved times contradicting its shape) arms
        nothing for that day. Already-armed instants are kept rather than re-created, so
        the midnight re-run can never cancel a boundary that is due at that same instant,
        and future timers that no longer match (the location changed) are cancelled.
        """
        now = dt_util.as_local(now)
        horizon = now + _SOLAR_ARM_HORIZON
        today = now.date()
        desired: dict[tuple[int, bool, datetime], ScheduleWindow] = {}
        for index, window in enumerate(self.windows):
            if not window.is_solar:
                continue
            for delta in range(-2, 4):
                day = today + timedelta(days=delta)
                resolved = window.resolve(day, self.sun_event)
                if isinstance(resolved, str):
                    if resolved and (index, day) not in self._reported_skips:
                        self._reported_skips.add((index, day))
                        _LOGGER.warning(
                            "Entry %s: %s window %s does not run: %s",
                            self.entry.title,
                            window.mode,
                            window.label,
                            resolved,
                        )
                    continue
                for entering, instant in ((True, resolved[0]), (False, resolved[1])):
                    if now < instant <= horizon:
                        desired[(index, entering, dt_util.as_utc(instant))] = window

        # Cancel only *future* timers that no longer apply. One that is already due but has
        # not run yet (a late midnight run, a busy loop) must be left to fire.
        now_utc = dt_util.as_utc(now)
        for key in [key for key in self._solar_timers if key not in desired and key[2] > now_utc]:
            self._solar_timers.pop(key)()
        for key, window in desired.items():
            if key in self._solar_timers:
                continue
            _index, entering, instant = key
            self._solar_timers[key] = async_track_point_in_time(
                self.hass, self._make_solar_callback(key, window, entering=entering), instant
            )
            _LOGGER.debug(
                "Entry %s: armed %s of %s window %s at %s",
                self.entry.title,
                "start" if entering else "end",
                window.mode,
                window.label,
                dt_util.as_local(instant).isoformat(),
            )
        # Forget skip reports for days long gone so the set stays bounded.
        self._reported_skips = {item for item in self._reported_skips if item[1] >= today - timedelta(days=3)}

    def _make_solar_callback(
        self, key: tuple[int, bool, datetime], window: ScheduleWindow, *, entering: bool
    ) -> Callable[[datetime], None]:
        """Build the one-shot ``async_track_point_in_time`` callback for a resolved solar boundary."""
        instant = dt_util.as_local(key[2])

        @callback
        def _on_solar_boundary(_now: datetime) -> None:
            self._solar_timers.pop(key, None)
            # Evaluate at the instant the boundary was resolved to, not the (possibly late)
            # moment the event loop got round to firing it.
            self._spawn(self._on_boundary_impl(window, entering=entering, at=instant))

        return _on_solar_boundary

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        # Track the task so async_stop can cancel a mode change that is still in flight.
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _make_transition_callback(self, window: ScheduleWindow, *, entering: bool) -> Callable[[datetime], None]:
        """Build the ``async_track_time_change`` callback for one boundary."""

        @callback
        def _on_boundary(_now: datetime) -> None:
            # ``async_track_time_change`` fires the callback synchronously; kick
            # the mode change into a task so we can await the select entity.
            self._spawn(self._on_boundary_impl(window, entering=entering))

        return _on_boundary

    async def _on_boundary_impl(self, window: ScheduleWindow, *, entering: bool, at: datetime | None = None) -> None:
        """Apply the mode for a boundary crossing.

        Fixed boundaries are armed for every day — ``async_track_time_change`` has no
        "Mondays only" form — so a weekday-masked window still receives both callbacks on
        the days it does not run, and each has to be checked before acting:

        * *entering*: the window must contain the boundary instant, or a Monday-only window
          would actuate every Tuesday and hold that mode until its next boundary;
        * *leaving*: the window must have been running a moment before the boundary, or its
          end callback would release the battery to Self-consumption every non-matching day,
          stomping whatever mode the user had set.

        Solar boundaries are only armed for days the window runs, but go through the same
        checks against their resolved times (``at``), so both paths share one rule.

        On *leaving* a window: revert to ``self_consumption`` — but only if the
        window we're leaving is still the ``_current_window``. If a longer
        window is nested inside a shorter one (overlap), the leaving-callback
        for the shorter one fires first, and we don't want it to release the
        battery while the enclosing window should still be active.
        """
        now = at if at is not None else dt_util.now()
        # One second before, because ``end`` is exclusive: at the boundary instant the
        # window has already closed, so ask whether it was still open just before it.
        was_running = self._contains(window, now - timedelta(seconds=1))
        if not (self._contains(window, now) if entering else was_running):
            _LOGGER.debug(
                "Entry %s: %s window %s does not run now; skipping its %s boundary",
                self.entry.title,
                window.mode,
                window.label,
                "start" if entering else "end",
            )
            return

        if entering:
            self._current_window = window
            _LOGGER.info("Entry %s: entering scheduled window %s (%s)", self.entry.title, window.label, window.mode)
            await self._apply_mode(window.mode)
            return

        # Leaving: if an enclosing (later-starting) window still applies at
        # ``now``, keep its mode; otherwise release the battery.
        still_active = self.active_window(now)
        if still_active is None:
            _LOGGER.info(
                "Entry %s: leaving scheduled window %s; releasing battery to Self-consumption",
                self.entry.title,
                window.label,
            )
            self._current_window = None
            await self._apply_mode(_MODE_AFTER_WINDOW)
        else:
            # The inner window overrode an enclosing one (latest start wins); when it
            # ends we must put the enclosing window's mode back, or the inverter keeps
            # the inner mode until the outer window also ends.
            _LOGGER.info(
                "Entry %s: window %s ended; re-applying enclosing window %s (%s)",
                self.entry.title,
                window.label,
                still_active.label,
                still_active.mode,
            )
            self._current_window = still_active
            if still_active.mode != window.mode:
                await self._apply_mode(still_active.mode)

    async def _apply_mode(self, mode_key: str) -> None:
        """Set the battery mode on every battery-mode select owned by this entry.

        Mirrors ``sungrow.set_battery_mode`` (#255) but scoped to this entry's
        plants so scheduling one entry doesn't sneakily touch another. Failures
        are logged and swallowed — a transient cloud hiccup shouldn't tear
        down the schedule; the next boundary or a manual invocation will
        retry.
        """
        from .select import BATTERY_MODE_PARAM, BATTERY_MODE_SERVICE_KEYS

        try:
            option = BATTERY_MODE_SERVICE_KEYS[mode_key]
        except KeyError:
            _LOGGER.warning("Scheduler received unknown mode key %r; skipping", mode_key)
            return

        registry = self.hass.data.get(DOMAIN, {}).get("battery_mode_selects")
        if not isinstance(registry, dict) or not registry:
            _LOGGER.debug(
                "Scheduler for entry %s: no battery-mode selects registered yet; skipping",
                self.entry.title,
            )
            return

        # Only touch selects that belong to *this* entry's coordinators, not
        # every entry's. The select's ``config_entry`` attribute (set on HA
        # entity registration) is the cheapest way to tell.
        for select in list(registry.values()):
            entity_entry = getattr(select, "registry_entry", None)
            if entity_entry is not None and entity_entry.config_entry_id != self.entry.entry_id:
                continue
            try:
                await select.async_select_option(option)
                if select.hass is not None and getattr(select, "platform", None) is not None:
                    select.async_write_ha_state()
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "Scheduler for entry %s: failed to set %s on %s: %s",
                    self.entry.title,
                    BATTERY_MODE_PARAM,
                    getattr(select, "entity_id", "<unknown>"),
                    err,
                )


def _parse_window(row: Any) -> ScheduleWindow:
    """Parse one schedule window dict into a :class:`ScheduleWindow`.

    Accepts the shape produced by the options flow and equivalent user-authored
    YAML — ``start`` / ``end`` as ``"HH:MM"`` strings (or ``time`` instances) or
    sun-relative ``"sunset-00:30"`` strings (see :func:`parse_boundary`), ``mode``
    as one of the accepted mode keys, and an optional ``days`` weekday mask
    (omitted or empty means every day).
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected a dict, got {type(row).__name__}")
    start = parse_boundary(row["start"])
    end = parse_boundary(row["end"])
    mode = str(row["mode"]).strip()
    return ScheduleWindow(start=start, end=end, mode=mode, days=_parse_days(row.get("days")))


def _parse_days(value: Any) -> frozenset[int] | None:
    """Coerce a window's ``days`` mask to weekday numbers, or ``None`` for every day.

    Accepts the three-letter names the options-flow multi-select emits (``["mon", "fri"]``)
    as well as full names, the numbers ``datetime.weekday()`` uses, and a comma- or
    space-separated string, so hand-authored options are as forgiving as the form. An
    absent, empty or all-seven selection means "every day" and is normalised to ``None``
    so the stored row stays clean.
    """
    if value is None:
        return None
    if isinstance(value, str):
        items: list[Any] = [part for part in value.replace(",", " ").split() if part]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        raise TypeError(f"Cannot coerce {value!r} ({type(value).__name__}) to a day mask")

    days: set[int] = set()
    for item in items:
        if isinstance(item, bool):
            raise ValueError(f"Invalid weekday {item!r}")
        if isinstance(item, int):
            days.add(item)
            continue
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in _DAY_NAMES:
            raise ValueError(f"Unknown weekday {item!r}; expected one of {', '.join(sorted(_DAY_NAMES))}")
        days.add(_DAY_NAMES[name])

    if not days or days == frozenset(range(7)):
        return None
    return frozenset(days)


def parse_boundary(value: Any) -> Boundary:
    """Parse a window boundary: a fixed local time or a sun-relative offset (#482).

    Sun-relative boundaries are ``sunrise`` or ``sunset``, optionally followed by a signed
    ``HH:MM`` (or ``HH:MM:SS``) offset of at most 12 hours — ``sunset``, ``sunset-00:30``,
    ``sunrise+1:15``. Case and spaces around the sign are ignored. Everything else goes
    through :func:`_coerce_time`, so stored fixed-time rows parse exactly as before.
    """
    if isinstance(value, SunBoundary):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text.startswith(SUN_EVENTS):
            match = _SUN_BOUNDARY_RE.match(text)
            if match is None:
                raise ValueError(f"Expected sunrise/sunset with an optional ±HH:MM offset, got {value!r}")
            event, sign, hours, minutes, seconds = match.groups()
            offset = timedelta(0)
            if sign is not None:
                if int(minutes) > 59 or int(seconds or 0) > 59:
                    raise ValueError(f"Invalid sun offset in {value!r}")
                offset = timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds or 0))
                if sign == "-":
                    offset = -offset
            return SunBoundary(event=event, offset=offset)
    return _coerce_time(value)


def _coerce_time(value: Any) -> time:
    """Coerce a value to a :class:`~datetime.time`.

    Accepts ``time`` instances (options-flow ``TimeSelector`` returns strings
    but user-authored YAML might produce ``time`` objects), and ``"HH:MM"`` /
    ``"HH:MM:SS"`` strings. Anything else raises so the caller drops the row
    and logs a warning.
    """
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        text = value.strip()
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Expected HH:MM or HH:MM:SS, got {value!r}")
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    raise TypeError(f"Cannot coerce {value!r} ({type(value).__name__}) to time")
