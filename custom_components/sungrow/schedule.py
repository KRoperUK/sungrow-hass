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

Deliberately scoped narrow for v1: **daily-repeating** windows only. Weekly
patterns and TOU tariff plans are out of scope (they belong on the Energy
dashboard or a separate feature).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change

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


@dataclass(frozen=True)
class ScheduleWindow:
    """A single repeating window that arms one battery mode.

    ``start`` and ``end`` are local wall-clock times. If ``start >= end`` the
    window wraps over midnight — ``23:30`` → ``06:00`` means "from 23:30 today
    until 06:00 tomorrow".

    ``days`` restricts the window to a set of weekdays (``datetime.weekday()``,
    Monday = 0). ``None`` means every day, which is what a window without a mask
    has always meant, so existing configurations are unaffected.
    """

    start: time
    end: time
    mode: str
    days: frozenset[int] | None = None

    def __post_init__(self) -> None:
        """Validate the mode and the optional weekday mask."""
        if self.mode not in _SCHEDULE_MODES:
            raise ValueError(f"Invalid schedule mode: {self.mode!r}")
        if self.start == self.end:
            raise ValueError(f"Window start ({self.start}) equals end ({self.end}); a zero-length window has no effect")
        if self.days is not None:
            if not self.days:
                raise ValueError("Window has an empty day mask; it would never run")
            invalid = sorted(day for day in self.days if day not in range(7))
            if invalid:
                raise ValueError(f"Invalid weekday numbers {invalid}; expected 0 (Monday) to 6 (Sunday)")

    def runs_on(self, weekday: int) -> bool:
        """Return True if this window runs on ``weekday`` (``datetime.weekday()``)."""
        return self.days is None or weekday in self.days

    def contains(self, moment: datetime) -> bool:
        """Return True if ``moment`` (local) is inside this window.

        Takes a full ``datetime`` rather than a ``time`` because a weekday mask has to
        know which day the window belongs to, and a wrapping window's small-hours leg
        belongs to the day it *started* on: a Monday-only ``23:30 → 06:00`` runs into
        Tuesday morning, and ``06:00`` Tuesday is still "Monday's" window.
        """
        now = moment.time()
        if self.start < self.end:
            return self.runs_on(moment.weekday()) and self.start <= now < self.end
        # Wraps over midnight: either tonight's leg (from ``start``) or yesterday's
        # spill into this morning. ``end`` is exclusive, so the closing boundary is out.
        if now >= self.start:
            return self.runs_on(moment.weekday())
        return now < self.end and self.runs_on((moment.weekday() - 1) % 7)


@dataclass
class SungrowScheduler:
    """Per-entry schedule engine (#359).

    Instantiated in :func:`~custom_components.sungrow.async_setup_entry` for
    every cloud entry with at least one battery-capable plant. On
    :meth:`async_start` it evaluates the currently-active window and applies
    the matching mode up front, then registers ``async_track_time_change``
    callbacks for every window boundary so future transitions actuate at the
    right time. :meth:`async_stop` releases every callback so unloading /
    reloading the entry is clean.
    """

    hass: HomeAssistant
    entry: SungrowConfigEntry
    windows: list[ScheduleWindow] = field(default_factory=list)
    _cancels: list[CALLBACK_TYPE] = field(default_factory=list, init=False, repr=False)
    _current_window: ScheduleWindow | None = field(default=None, init=False, repr=False)
    # In-flight boundary tasks, so a reload/unload can cancel a mode change that is
    # mid-write instead of letting it actuate against a torn-down entry.
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)

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

    def active_window(self, moment: datetime) -> ScheduleWindow | None:
        """Return the schedule window active at ``moment`` (local), or None.

        Windows whose weekday mask excludes ``moment``'s day are skipped.

        Overlap policy: multiple windows can match a given time; the one with
        the latest ``start`` wins. That's the intuitive "most recently entered
        wins" behaviour — a superset window fully containing a shorter one
        gets overridden during the shorter window's span. Wrap-over-midnight
        starts count as their raw ``start`` time (a 23:30-06:00 window's start
        is 23:30, later than a 08:00 window's start).
        """
        matches = [w for w in self.windows if w.contains(moment)]
        if not matches:
            return None
        return max(matches, key=lambda w: w.start)

    async def async_start(self) -> None:
        """Apply the currently-active window's mode and arm boundary callbacks.

        Idempotent: safe to call twice — a second call is treated as a reload
        and drops any callbacks the previous call installed. That's what lets
        the options-flow's ``OptionsFlowWithReload`` reload the entry without
        leaking stale timers.
        """
        self.async_stop()
        if not self.windows:
            _LOGGER.debug("Scheduler for entry %s has no windows; nothing to arm", self.entry.title)
            return

        # Apply the mode active *right now* so an HA restart mid-window doesn't
        # leave the inverter drifting outside the intended mode.
        from homeassistant.util import dt as dt_util

        now_local = dt_util.now()
        self._current_window = self.active_window(now_local)
        if self._current_window is not None:
            _LOGGER.info(
                "Entry %s: applying scheduled mode %s (window %s-%s) on setup",
                self.entry.title,
                self._current_window.mode,
                self._current_window.start.strftime("%H:%M"),
                self._current_window.end.strftime("%H:%M"),
            )
            await self._apply_mode(self._current_window.mode)
        else:
            _LOGGER.debug("Entry %s: no schedule window active at %s", self.entry.title, now_local.time())

        # Arm one time-change callback per window boundary. ``async_track_time_change``
        # fires every day at the specified HH:MM:00, so we get daily repetition for free
        # without having to reschedule after each fire.
        for i, window in enumerate(self.windows):
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
            _LOGGER.debug(
                "Entry %s: armed schedule window #%d (%s %s-%s)",
                self.entry.title,
                i,
                window.mode,
                window.start.strftime("%H:%M"),
                window.end.strftime("%H:%M"),
            )

    @callback
    def async_stop(self) -> None:
        """Cancel every armed transition callback and in-flight task. Idempotent."""
        for cancel in self._cancels:
            cancel()
        self._cancels.clear()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._current_window = None

    def _make_transition_callback(self, window: ScheduleWindow, *, entering: bool) -> Callable[[datetime], None]:
        """Build the ``async_track_time_change`` callback for one boundary."""

        @callback
        def _on_boundary(_now: datetime) -> None:
            # ``async_track_time_change`` fires the callback synchronously; kick
            # the mode change into a task so we can await the select entity. Track
            # it so async_stop can cancel a mode change that is still in flight.
            task = self.hass.async_create_task(self._on_boundary_impl(window, entering=entering))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return _on_boundary

    async def _on_boundary_impl(self, window: ScheduleWindow, *, entering: bool) -> None:
        """Apply the mode for a boundary crossing.

        Boundaries are armed for every day — ``async_track_time_change`` has no "Mondays
        only" form — so a weekday-masked window still receives both callbacks on the days it
        does not run, and each has to be checked before acting:

        * *entering*: the window must contain the boundary instant, or a Monday-only window
          would actuate every Tuesday and hold that mode until its next boundary;
        * *leaving*: the window must have been running a moment before the boundary, or its
          end callback would release the battery to Self-consumption every non-matching day,
          stomping whatever mode the user had set.

        On *leaving* a window: revert to ``self_consumption`` — but only if the
        window we're leaving is still the ``_current_window``. If a longer
        window is nested inside a shorter one (overlap), the leaving-callback
        for the shorter one fires first, and we don't want it to release the
        battery while the enclosing window should still be active.
        """
        from homeassistant.util import dt as dt_util

        now = dt_util.now()
        # One second before, because ``end`` is exclusive: at the boundary instant the
        # window has already closed, so ask whether it was still open just before it.
        was_running = window.contains(now - timedelta(seconds=1))
        if not (window.contains(now) if entering else was_running):
            _LOGGER.debug(
                "Entry %s: %s window %s-%s does not run now; skipping its %s boundary",
                self.entry.title,
                window.mode,
                window.start.strftime("%H:%M"),
                window.end.strftime("%H:%M"),
                "start" if entering else "end",
            )
            return

        if entering:
            self._current_window = window
            _LOGGER.info(
                "Entry %s: entering scheduled window %s-%s (%s)",
                self.entry.title,
                window.start.strftime("%H:%M"),
                window.end.strftime("%H:%M"),
                window.mode,
            )
            await self._apply_mode(window.mode)
            return

        # Leaving: if an enclosing (later-starting) window still applies at
        # ``now``, keep its mode; otherwise release the battery.
        still_active = self.active_window(now)
        if still_active is None:
            _LOGGER.info(
                "Entry %s: leaving scheduled window %s-%s; releasing battery to Self-consumption",
                self.entry.title,
                window.start.strftime("%H:%M"),
                window.end.strftime("%H:%M"),
            )
            self._current_window = None
            await self._apply_mode(_MODE_AFTER_WINDOW)
        else:
            # The inner window overrode an enclosing one (latest start wins); when it
            # ends we must put the enclosing window's mode back, or the inverter keeps
            # the inner mode until the outer window also ends.
            _LOGGER.info(
                "Entry %s: window %s-%s ended; re-applying enclosing window %s-%s (%s)",
                self.entry.title,
                window.start.strftime("%H:%M"),
                window.end.strftime("%H:%M"),
                still_active.start.strftime("%H:%M"),
                still_active.end.strftime("%H:%M"),
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
    YAML — ``start`` / ``end`` as ``"HH:MM"`` strings (or ``time`` instances),
    ``mode`` as one of the accepted mode keys, and an optional ``days`` weekday
    mask (omitted or empty means every day).
    """
    if not isinstance(row, dict):
        raise TypeError(f"Expected a dict, got {type(row).__name__}")
    start = _coerce_time(row["start"])
    end = _coerce_time(row["end"])
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
