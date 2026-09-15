"""Unit tests for the API call-rate tracker (#434)."""

from custom_components.sungrow.api_rate import (
    CALL_TYPE_DEVICE_REALTIME,
    CALL_TYPE_PLANT_DETAIL,
    CALL_TYPE_REALTIME,
    CALL_TYPE_USER_DEVICE_FETCH,
    ApiCallRateTracker,
    label_for,
)


class _FakeClock:
    """A manually-advanced monotonic clock for deterministic window tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tracker(clock: _FakeClock, **kwargs) -> ApiCallRateTracker:
    return ApiCallRateTracker(time_fn=clock, **kwargs)


# ---------------------------------------------------------------------------
# Counting by type
# ---------------------------------------------------------------------------


def test_counts_by_type_tallies_each_call_type():
    """Each recorded call is tallied under its own type."""
    clock = _FakeClock()
    tracker = _tracker(clock)
    tracker.record(CALL_TYPE_REALTIME)
    tracker.record(CALL_TYPE_REALTIME)
    tracker.record(CALL_TYPE_PLANT_DETAIL)

    assert tracker.counts_by_type() == {CALL_TYPE_REALTIME: 2, CALL_TYPE_PLANT_DETAIL: 1}
    assert tracker.total_in_window() == 3


def test_empty_tracker_reports_zero_and_no_dominant():
    """A fresh tracker has no calls, no dominant type, and is not approaching budget."""
    tracker = _tracker(_FakeClock())
    assert tracker.total_in_window() == 0
    assert tracker.counts_by_type() == {}
    assert tracker.dominant_type() is None
    assert tracker.observed_rate_per_hour() == 0
    assert tracker.is_approaching_budget() is False
    assert tracker.as_diagnostics()["dominant_call_type"] is None


# ---------------------------------------------------------------------------
# Rolling-window expiry
# ---------------------------------------------------------------------------


def test_events_expire_out_of_the_trailing_window():
    """Calls older than the window no longer count toward the rate."""
    clock = _FakeClock()
    tracker = _tracker(clock, window_seconds=3600)
    for _ in range(5):
        tracker.record(CALL_TYPE_REALTIME)
    assert tracker.total_in_window() == 5

    # Advance just past the window: the old events age out.
    clock.advance(3601)
    assert tracker.total_in_window() == 0
    assert tracker.counts_by_type() == {}


def test_partial_window_expiry_keeps_recent_events():
    """Only the events older than the window are pruned; recent ones remain."""
    clock = _FakeClock()
    tracker = _tracker(clock, window_seconds=3600)
    tracker.record(CALL_TYPE_REALTIME)  # t=0
    clock.advance(1800)
    tracker.record(CALL_TYPE_REALTIME)  # t=1800
    clock.advance(1801)  # t=3601: the first event is now older than 3600 s
    assert tracker.total_in_window() == 1


# ---------------------------------------------------------------------------
# Rate scaling
# ---------------------------------------------------------------------------


def test_rate_scales_a_sub_hour_window_to_calls_per_hour():
    """A count over a shorter window is scaled up to a calls-per-hour figure."""
    clock = _FakeClock()
    tracker = _tracker(clock, window_seconds=60)  # 1-minute window
    for _ in range(10):
        tracker.record(CALL_TYPE_REALTIME)
    # 10 calls in 60 s -> 600 calls/hour.
    assert tracker.observed_rate_per_hour() == 600
    assert tracker.rates_by_type_per_hour() == {CALL_TYPE_REALTIME: 600}


def test_hour_window_rate_equals_count():
    """With a one-hour window the in-window count is already the hourly rate."""
    tracker = _tracker(_FakeClock(), window_seconds=3600)
    for _ in range(42):
        tracker.record(CALL_TYPE_PLANT_DETAIL)
    assert tracker.observed_rate_per_hour() == 42


# ---------------------------------------------------------------------------
# Dominant type
# ---------------------------------------------------------------------------


def test_dominant_type_is_the_biggest_contributor():
    """The dominant type is the one with the most calls in the window."""
    tracker = _tracker(_FakeClock())
    for _ in range(3):
        tracker.record(CALL_TYPE_REALTIME)
    for _ in range(7):
        tracker.record(CALL_TYPE_USER_DEVICE_FETCH)
    assert tracker.dominant_type() == CALL_TYPE_USER_DEVICE_FETCH


def test_dominant_type_breaks_ties_deterministically():
    """A tie resolves to the alphabetically-first type for a stable message."""
    tracker = _tracker(_FakeClock())
    tracker.record(CALL_TYPE_REALTIME)
    tracker.record(CALL_TYPE_DEVICE_REALTIME)
    # "device_realtime" sorts before "realtime".
    assert tracker.dominant_type() == CALL_TYPE_DEVICE_REALTIME


# ---------------------------------------------------------------------------
# Threshold crossing
# ---------------------------------------------------------------------------


def test_is_approaching_budget_crosses_at_the_threshold():
    """The tracker flags 'approaching' at (not before) the warning threshold."""
    tracker = _tracker(_FakeClock(), budget_per_hour=100, warn_fraction=0.8, window_seconds=3600)
    assert tracker.warn_threshold_per_hour == 80
    for _ in range(79):
        tracker.record(CALL_TYPE_REALTIME)
    assert tracker.is_approaching_budget() is False
    tracker.record(CALL_TYPE_REALTIME)  # 80th -> at threshold
    assert tracker.is_approaching_budget() is True


def test_approaching_budget_recovers_once_events_expire():
    """Once the burst ages out of the window, the tracker no longer warns."""
    clock = _FakeClock()
    tracker = _tracker(clock, budget_per_hour=100, warn_fraction=0.8, window_seconds=3600)
    for _ in range(90):
        tracker.record(CALL_TYPE_REALTIME)
    assert tracker.is_approaching_budget() is True
    clock.advance(3601)
    assert tracker.is_approaching_budget() is False


# ---------------------------------------------------------------------------
# Diagnostics snapshot + labels
# ---------------------------------------------------------------------------


def test_as_diagnostics_reports_per_type_rate_and_budget():
    """The diagnostics snapshot carries the per-type rate, budget and dominant type."""
    clock = _FakeClock()
    tracker = _tracker(clock, budget_per_hour=2000, warn_fraction=0.8, window_seconds=3600)
    for _ in range(100):
        tracker.record(CALL_TYPE_REALTIME)
    for _ in range(50):
        tracker.record(CALL_TYPE_PLANT_DETAIL)

    diag = tracker.as_diagnostics()
    assert diag["budget_per_hour"] == 2000
    assert diag["warn_threshold_per_hour"] == 1600
    assert diag["observed_rate_per_hour"] == 150
    assert diag["approaching_budget"] is False
    assert diag["dominant_call_type"] == CALL_TYPE_REALTIME
    assert diag["by_call_type"] == {CALL_TYPE_PLANT_DETAIL: 50, CALL_TYPE_REALTIME: 100}


def test_label_for_known_and_unknown_types():
    """Known call types get a friendly label; unknown ones pass through unchanged."""
    assert label_for(CALL_TYPE_USER_DEVICE_FETCH) == "per-poll device-list fetch (user account)"
    assert label_for("something_new") == "something_new"
