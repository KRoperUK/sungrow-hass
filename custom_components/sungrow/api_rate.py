"""Proactive iSolarCloud API call-rate tracking (#434).

The free iSolarCloud plan has an hourly request budget (of the order of 2000 calls
per hour); the E999 rejection is retroactive — the user only finds out the budget
is spent when data stops updating. This module tracks the request rate the
integration *itself* generates, tagged by call type, over a trailing window, so the
coordinator can warn (log + Repair) as the observed rate approaches the budget,
naming which call type dominates — *before* the API starts rejecting.

The tracker is deliberately pure and cheap: a bounded rolling window of
``(timestamp, call_type)`` events, evaluated lazily on each poll. It has no Home
Assistant knowledge (no timers, tasks or issue-registry access) so it is trivially
unit-testable with an injected clock; the coordinator owns the HA-facing warning /
Repair wiring. Only cloud transports create one — Modbus makes no API calls.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable

# Call-type tags. Each names a distinct place the integration spends the budget so a
# warning can point at the dominant contributor (#434 / #439).
CALL_TYPE_REALTIME = "realtime"
CALL_TYPE_DEVICE_LIST = "device_list"
CALL_TYPE_PLANT_DETAIL = "plant_detail"
CALL_TYPE_DEVICE_REALTIME = "device_realtime"
CALL_TYPE_USER_DEVICE_FETCH = "user_device_fetch"
CALL_TYPE_CONTROL = "control"

# Human-readable labels for the call types, used in the warning / Repair text so the
# message reads naturally rather than exposing the internal tag.
CALL_TYPE_LABELS: dict[str, str] = {
    CALL_TYPE_REALTIME: "realtime plant poll",
    CALL_TYPE_DEVICE_LIST: "device-list refresh",
    CALL_TYPE_PLANT_DETAIL: "plant-detail refresh",
    CALL_TYPE_DEVICE_REALTIME: "per-device data fetch",
    CALL_TYPE_USER_DEVICE_FETCH: "per-poll device-list fetch (user account)",
    CALL_TYPE_CONTROL: "dispatch control write",
}

# Documented free-plan hourly request budget. The E999 rejection is the *hourly* API
# call limit, so the tracker's trailing window and budget are both hour-scaled.
API_HOURLY_BUDGET = 2000

# Warn once the observed hourly rate reaches this fraction of the budget. 0.8 leaves
# real headroom (~400 calls/h on the default budget) to raise the Repair and let the
# user act before the API actually starts rejecting.
RATE_WARNING_FRACTION = 0.8

# Trailing window (seconds) the rate is measured over. One hour matches the E999
# hourly quota, so an in-window event count *is* the observed calls-per-hour.
WINDOW_SECONDS = 3600


def label_for(call_type: str) -> str:
    """Return the human-readable label for a call type (falls back to the raw tag)."""
    return CALL_TYPE_LABELS.get(call_type, call_type)


class ApiCallRateTracker:
    """A bounded rolling-window counter of outbound API calls, tagged by call type.

    ``record(call_type)`` appends an event; expired events (older than the window) are
    pruned lazily on record and on every query, so the structure stays bounded without
    any background task. All rates are expressed as calls-per-hour, scaled from the
    in-window count so a sub-hour window still reports a comparable figure.
    """

    def __init__(
        self,
        *,
        budget_per_hour: int = API_HOURLY_BUDGET,
        warn_fraction: float = RATE_WARNING_FRACTION,
        window_seconds: float = WINDOW_SECONDS,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialise the tracker.

        ``time_fn`` is injectable so tests can drive a deterministic clock; production
        passes the event loop's monotonic clock (``hass.loop.time``).
        """
        self._budget = int(budget_per_hour)
        self._warn_fraction = float(warn_fraction)
        self._window = float(window_seconds)
        self._time_fn = time_fn
        self._events: deque[tuple[float, str]] = deque()
        # Latch so the coordinator logs the "approaching budget" warning once per
        # crossing rather than on every poll. Shared here (not per-coordinator) because
        # one tracker is shared across a config entry's plant coordinators.
        self.warning_active = False

    @property
    def budget_per_hour(self) -> int:
        """The documented hourly call budget."""
        return self._budget

    @property
    def warn_threshold_per_hour(self) -> float:
        """The calls-per-hour rate at (or above) which a warning is raised."""
        return self._budget * self._warn_fraction

    def _prune(self, now: float) -> None:
        """Drop events that have aged out of the trailing window."""
        cutoff = now - self._window
        events = self._events
        while events and events[0][0] <= cutoff:
            events.popleft()

    def record(self, call_type: str) -> None:
        """Record one outbound API call of the given type."""
        now = self._time_fn()
        self._events.append((now, call_type))
        self._prune(now)

    def _scale(self, count: float) -> float:
        """Scale an in-window count to a calls-per-hour rate."""
        if self._window <= 0:
            return float(count)
        return count * (3600.0 / self._window)

    def total_in_window(self) -> int:
        """Return the number of calls recorded within the trailing window."""
        self._prune(self._time_fn())
        return len(self._events)

    def counts_by_type(self) -> dict[str, int]:
        """Return the in-window call count per call type."""
        self._prune(self._time_fn())
        return dict(Counter(call_type for _, call_type in self._events))

    def observed_rate_per_hour(self) -> float:
        """Return the total observed rate in calls-per-hour."""
        return self._scale(self.total_in_window())

    def rates_by_type_per_hour(self) -> dict[str, float]:
        """Return the observed rate in calls-per-hour, per call type."""
        return {call_type: self._scale(count) for call_type, count in self.counts_by_type().items()}

    def dominant_type(self) -> str | None:
        """Return the call type contributing the most calls, or None when idle.

        Ties break deterministically by the call type's name so the reported dominant
        type is stable across polls.
        """
        counts = self.counts_by_type()
        if not counts:
            return None
        return max(sorted(counts), key=lambda call_type: counts[call_type])

    def is_approaching_budget(self) -> bool:
        """True once the observed hourly rate reaches the warning threshold."""
        return self.observed_rate_per_hour() >= self.warn_threshold_per_hour

    def as_diagnostics(self) -> dict[str, object]:
        """Return a JSON-safe snapshot of the observed rate for the diagnostics dump."""
        rates = self.rates_by_type_per_hour()
        dominant = self.dominant_type()
        return {
            "budget_per_hour": self._budget,
            "warn_threshold_per_hour": round(self.warn_threshold_per_hour, 1),
            "window_seconds": int(self._window),
            "observed_rate_per_hour": round(self.observed_rate_per_hour(), 1),
            "approaching_budget": self.is_approaching_budget(),
            "dominant_call_type": dominant,
            "by_call_type": {call_type: round(rate, 1) for call_type, rate in sorted(rates.items())},
        }
