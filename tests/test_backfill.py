"""Unit / example tests for the Backfill I/O orchestration shell.

Feature: backfill-historical-statistics.

These cover the pieces that carry I/O or wiring and are exercised with representative
examples and mocks (the combinatorial input space is owned by the property tests in
``test_backfill_properties.py``): series resolution against the entity registry, the
idempotent recorder-import router, and the shared Throttle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pysolarcloud import AuthError, PySolarCloudException

from custom_components.sungrow.backfill import (
    BackfillEngine,
    BackfillManager,
    SeriesTarget,
    Throttle,
    async_resolve_series,
    build_series_target,
    import_statistics,
    select_backfill_points,
)
from custom_components.sungrow.const import (
    BACKFILL_CHUNK_WINDOW,
    BACKFILL_INTERVAL,
    BACKFILL_MAX_RETRIES,
    CONF_BACKFILL_DAYS,
    DOMAIN,
)

# Two catalogued points: a cumulative-energy yield and an instantaneous power point.
_MEASURE_POINTS = {"83024": "total_yield", "83033": "power"}


def _fake_coordinator(plant_id: str = "123", measure_points: dict | None = None):
    """A minimal coordinator stand-in exposing plant_id and plants_service.measure_points."""
    return SimpleNamespace(
        plant_id=plant_id,
        plants_service=SimpleNamespace(
            measure_points=measure_points if measure_points is not None else dict(_MEASURE_POINTS)
        ),
    )


# ---------------------------------------------------------------------------
# Task 7 - series resolution and Statistic_Id derivation
# ---------------------------------------------------------------------------


def test_select_backfill_points_picks_energy_and_power():
    """Only cumulative-energy and power points are selected, tagged with their kind."""
    selected = select_backfill_points(_MEASURE_POINTS)
    assert ("total_yield", "energy") in selected
    assert ("power", "power") in selected


def test_build_series_target_energy_metadata_shape():
    """Energy live series -> recorder source, has_sum, unit from the live entity (7.1, 7.5)."""
    target = build_series_target(
        plant_id="123",
        point_code="total_yield",
        kind="energy",
        entity_id="sensor.plant_total_yield",
        unit="kWh",
    )
    assert target == SeriesTarget(
        point_code="total_yield",
        statistic_id="sensor.plant_total_yield",
        unit="kWh",
        kind="energy",
        is_external=False,
        metadata={
            "has_mean": False,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.plant_total_yield",
            "unit_of_measurement": "kWh",
        },
    )


def test_build_series_target_power_metadata_shape():
    """Power series -> has_mean, W default unit when the live entity supplies none (7.2)."""
    target = build_series_target(
        plant_id="123",
        point_code="power",
        kind="power",
        entity_id="sensor.plant_power",
        unit=None,
    )
    assert target.metadata["has_mean"] is True
    assert target.metadata["has_sum"] is False
    assert target.unit == "W"
    assert target.metadata["unit_of_measurement"] == "W"
    assert target.is_external is False


def test_build_series_target_external_fallback():
    """No live entity -> per-plant external series with the DOMAIN source (7.4, 9.2)."""
    target = build_series_target(
        plant_id="123",
        point_code="total_yield",
        kind="energy",
        entity_id=None,
        unit=None,
    )
    assert target.is_external is True
    assert target.statistic_id == "sungrow:123_total_yield"
    assert target.metadata["source"] == DOMAIN
    assert target.unit == "kWh"


@pytest.mark.asyncio
async def test_async_resolve_series_live_entity(hass: HomeAssistant):
    """A registered sensor resolves to a live-entity series with its live unit (7.3, 7.5)."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create("sensor", DOMAIN, "123_total_yield", unit_of_measurement="kWh")
    # A live state overrides the registry unit; use it to prove state-first resolution.
    hass.states.async_set(entry.entity_id, "1234.0", {"unit_of_measurement": "kWh"})

    targets = await async_resolve_series(hass, _fake_coordinator())
    by_code = {t.point_code: t for t in targets}

    yield_target = by_code["total_yield"]
    assert yield_target.is_external is False
    assert yield_target.statistic_id == entry.entity_id
    assert yield_target.unit == "kWh"
    assert yield_target.metadata["source"] == "recorder"
    assert yield_target.kind == "energy"


@pytest.mark.asyncio
async def test_async_resolve_series_external_fallback(hass: HomeAssistant):
    """With an empty registry every point falls back to an external series (7.4)."""
    targets = await async_resolve_series(hass, _fake_coordinator())
    by_code = {t.point_code: t for t in targets}

    assert by_code["total_yield"].is_external is True
    assert by_code["total_yield"].statistic_id == "sungrow:123_total_yield"
    assert by_code["power"].is_external is True
    assert by_code["power"].statistic_id == "sungrow:123_power"
    assert by_code["power"].kind == "power"


@pytest.mark.asyncio
async def test_async_resolve_series_scopes_per_plant(hass: HomeAssistant):
    """Two plants with the same code get distinct external ids (9.2)."""
    a = await async_resolve_series(hass, _fake_coordinator("111"))
    b = await async_resolve_series(hass, _fake_coordinator("222"))
    id_a = {t.point_code: t.statistic_id for t in a}["total_yield"]
    id_b = {t.point_code: t.statistic_id for t in b}["total_yield"]
    assert id_a != id_b


# ---------------------------------------------------------------------------
# Task 8 - idempotent import router
# ---------------------------------------------------------------------------


class _FakeRecorder:
    """A fake (statistic_id, start_hour)-keyed store mimicking the recorder helpers."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, datetime], dict] = {}
        self.calls = 0

    def add(self, hass, metadata, data) -> None:  # matches recorder helper signature
        self.calls += 1
        for row in data:
            self.store[(metadata["statistic_id"], row["start"])] = row


def _hour(h: int) -> datetime:
    return datetime(2024, 1, 1, h, 0, 0, tzinfo=UTC)


@pytest.fixture
def fake_recorder():
    recorder = _FakeRecorder()
    with (
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            side_effect=recorder.add,
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_add_external_statistics",
            side_effect=recorder.add,
        ),
    ):
        yield recorder


def test_import_statistics_reimport_overwrites(hass: HomeAssistant, fake_recorder):
    """Re-importing the same hours overwrites rather than duplicating (6.1, 6.2)."""
    target = build_series_target(plant_id="123", point_code="total_yield", kind="energy", entity_id=None, unit=None)
    data = [
        {"start": _hour(0), "state": 1.0, "sum": 1.0},
        {"start": _hour(1), "state": 2.0, "sum": 2.0},
    ]

    import_statistics(hass, target, data)
    import_statistics(hass, target, data)

    assert len(fake_recorder.store) == 2
    assert fake_recorder.store[(target.statistic_id, _hour(1))]["sum"] == 2.0


def test_import_statistics_retried_chunk_is_hour_local(hass: HomeAssistant, fake_recorder):
    """A retried chunk only touches its own hours and leaves earlier hours intact (6.4)."""
    target = build_series_target(plant_id="123", point_code="total_yield", kind="energy", entity_id=None, unit=None)
    chunk_a = [{"start": _hour(0), "state": 1.0, "sum": 1.0}]
    chunk_b = [{"start": _hour(1), "state": 5.0, "sum": 5.0}]

    import_statistics(hass, target, chunk_a)
    import_statistics(hass, target, chunk_b)
    import_statistics(hass, target, chunk_b)  # retry chunk B

    assert set(fake_recorder.store.keys()) == {
        (target.statistic_id, _hour(0)),
        (target.statistic_id, _hour(1)),
    }
    assert fake_recorder.store[(target.statistic_id, _hour(0))]["sum"] == 1.0


def test_import_statistics_routes_live_vs_external(hass: HomeAssistant):
    """External targets route to the external helper; live targets to the import helper."""
    external = build_series_target(plant_id="123", point_code="total_yield", kind="energy", entity_id=None, unit=None)
    live = build_series_target(
        plant_id="123",
        point_code="total_yield",
        kind="energy",
        entity_id="sensor.plant_total_yield",
        unit="kWh",
    )
    data = [{"start": _hour(0), "state": 1.0, "sum": 1.0}]

    with (
        patch("homeassistant.components.recorder.statistics.async_import_statistics") as mock_import,
        patch("homeassistant.components.recorder.statistics.async_add_external_statistics") as mock_external,
    ):
        import_statistics(hass, external, data)
        import_statistics(hass, live, data)
        import_statistics(hass, external, [])  # empty is a no-op

    mock_external.assert_called_once()
    mock_import.assert_called_once()


# ---------------------------------------------------------------------------
# Task 9 - shared Throttle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_spaces_calls_by_min_interval():
    """acquire waits out the remaining min_interval since the previous acquire (5.1)."""
    throttle = Throttle(min_interval=1.0)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    throttle._sleep = fake_sleep  # type: ignore[method-assign]

    with patch("custom_components.sungrow.backfill.time.monotonic") as mono:
        mono.return_value = 100.0
        await throttle.acquire()  # first call: no wait
        mono.return_value = 100.3  # only 0.3s elapsed
        await throttle.acquire()  # must wait the remaining 0.7s

    assert sleeps == [pytest.approx(0.7)]


@pytest.mark.asyncio
async def test_throttle_no_wait_when_interval_elapsed():
    """No sleep when at least min_interval has already passed."""
    throttle = Throttle(min_interval=1.0)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    throttle._sleep = fake_sleep  # type: ignore[method-assign]

    with patch("custom_components.sungrow.backfill.time.monotonic") as mono:
        mono.return_value = 100.0
        await throttle.acquire()
        mono.return_value = 105.0  # 5s elapsed > interval
        await throttle.acquire()

    assert sleeps == []


@pytest.mark.asyncio
async def test_throttle_is_shared_across_engines():
    """A single shared instance spaces calls made by two different engines (9.4)."""
    shared = Throttle(min_interval=2.0)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    shared._sleep = fake_sleep  # type: ignore[method-assign]

    with patch("custom_components.sungrow.backfill.time.monotonic") as mono:
        mono.return_value = 50.0
        await shared.acquire()  # "engine A"
        mono.return_value = 50.5
        await shared.acquire()  # "engine B" sees engine A's timestamp

    assert sleeps == [pytest.approx(1.5)]


@pytest.mark.asyncio
async def test_throttle_backoff_escalates_and_caps_at_one_hour():
    """backoff doubles each call and is capped at 3600s; reset clears it (5.2)."""
    throttle = Throttle(min_interval=1.0)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    throttle._sleep = fake_sleep  # type: ignore[method-assign]

    for _ in range(15):
        await throttle.backoff()

    # Monotonic non-decreasing, doubling until the 1-hour cap, then pinned at the cap.
    assert sleeps[0] == 1.0
    assert sleeps[1] == 2.0
    assert sleeps[2] == 4.0
    for earlier, later in zip(sleeps, sleeps[1:], strict=False):
        assert later >= earlier
    assert max(sleeps) == 3600.0
    assert sleeps[-1] == 3600.0

    # reset_backoff restarts the escalation from the floor.
    throttle.reset_backoff()
    sleeps.clear()
    await throttle.backoff()
    assert sleeps == [1.0]


# ---------------------------------------------------------------------------
# Task 11 - BackfillEngine.async_run run loop
# ---------------------------------------------------------------------------


def _engine_coordinator(*, option_days: int | None = 1, plant_id: str = "123"):
    """A coordinator stand-in with a config entry and a plants_service for the engine."""
    options: dict = {}
    if option_days is not None:
        options[CONF_BACKFILL_DAYS] = option_days
    return SimpleNamespace(
        plant_id=plant_id,
        config_entry=SimpleNamespace(options=options),
        plants_service=SimpleNamespace(
            measure_points=dict(_MEASURE_POINTS),
            async_get_historical_data=AsyncMock(),
        ),
    )


def _make_engine(hass: HomeAssistant, coordinator):
    """Build a BackfillEngine with a non-sleeping throttle and a marker-recording store."""
    throttle = Throttle(min_interval=0.0)
    throttle._sleep = AsyncMock()  # type: ignore[method-assign]
    store = SimpleNamespace(async_set_marker=AsyncMock())
    engine = BackfillEngine(hass, coordinator, throttle, store)
    engine._sleep = AsyncMock()  # type: ignore[method-assign]
    return engine, throttle, store


def _row(code: str, ts: datetime, value: float):
    return {
        "timestamp": ts,
        "id": code,
        "code": code,
        "value": value,
        "unit": "kWh" if code == "total_yield" else "W",
        "name": code,
    }


@pytest.mark.asyncio
async def test_engine_happy_path_ascending_chunks(hass: HomeAssistant, caplog):
    """Ascending chunk loop, explicit bounding call kwargs (4.3), and RunSummary counts (8.6)."""
    coordinator = _engine_coordinator(option_days=1)
    calls: list[tuple] = []

    async def history(plant_id, start, end, *, measure_points, interval):
        calls.append((start, end, tuple(measure_points), interval))
        return {plant_id: [_row(c, start, 100.0) for c in measure_points]}

    coordinator.plants_service.async_get_historical_data.side_effect = history
    engine, _throttle, store = _make_engine(hass, coordinator)

    with (
        patch("custom_components.sungrow.backfill.import_statistics"),
        caplog.at_level(logging.INFO, logger="custom_components.sungrow.backfill"),
    ):
        summary = await engine.async_run()

    # A 1-day window in 3-hour chunks is exactly 8 calls, in ascending chronological order.
    assert len(calls) == 8
    starts = [c[0] for c in calls]
    assert starts == sorted(starts)

    # Every call bounds a single chunk (<= 3h) with the 5-minute interval and both codes (4.3).
    for start, end, mps, interval in calls:
        assert end - start <= BACKFILL_CHUNK_WINDOW
        assert interval == BACKFILL_INTERVAL
        assert mps == ("total_yield", "power")

    # Two series x 8 distinct chunk-start hours = 16 imported hours; run completed cleanly (8.6).
    assert summary.imported_hours == 16
    assert summary.skipped_empty_ranges == 0
    assert summary.failed_chunks == 0
    assert summary.completed is True
    assert summary.plant_id == "123"

    # Start and outcome are logged (8.4), and a completed marker is persisted.
    assert "starting" in caplog.text
    assert "finished" in caplog.text
    marker = store.async_set_marker.await_args.args[1]
    assert marker["completed"] is True
    assert marker["failed_chunks"] == 0


@pytest.mark.asyncio
async def test_engine_empty_ranges_are_skipped(hass: HomeAssistant, caplog):
    """A call returning no rows is counted as an empty range and logged at debug (8.3)."""
    coordinator = _engine_coordinator(option_days=1)
    coordinator.plants_service.async_get_historical_data.return_value = {}
    engine, _throttle, _store = _make_engine(hass, coordinator)

    with (
        patch("custom_components.sungrow.backfill.import_statistics") as mock_import,
        caplog.at_level(logging.DEBUG, logger="custom_components.sungrow.backfill"),
    ):
        summary = await engine.async_run()

    assert summary.imported_hours == 0
    assert summary.skipped_empty_ranges == 8
    assert summary.failed_chunks == 0
    assert summary.completed is True
    mock_import.assert_not_called()
    assert "returned no rows" in caplog.text


@pytest.mark.asyncio
async def test_engine_rate_limit_backs_off_and_resumes_from_cursor(hass: HomeAssistant):
    """A rate-limit backs off and retries the SAME (chunk, batch) without advancing (5.2, 5.3)."""
    coordinator = _engine_coordinator(option_days=1)
    state = {"raised": False}

    async def history(plant_id, start, end, *, measure_points, interval):
        if not state["raised"]:
            state["raised"] = True
            raise PySolarCloudException({"result_code": "E999"})
        return {plant_id: [_row(c, start, 100.0) for c in measure_points]}

    coordinator.plants_service.async_get_historical_data.side_effect = history
    engine, throttle, _store = _make_engine(hass, coordinator)

    with patch("custom_components.sungrow.backfill.import_statistics"):
        summary = await engine.async_run()

    # 8 chunks + one retried first chunk = 9 calls; the run still completes.
    assert coordinator.plants_service.async_get_historical_data.await_count == 9
    throttle._sleep.assert_awaited()  # backoff slept
    assert summary.failed_chunks == 0
    assert summary.completed is True
    assert summary.imported_hours == 16


@pytest.mark.asyncio
async def test_engine_transient_retries_then_marks_chunk_failed(hass: HomeAssistant):
    """Bounded transient retries, then the chunk is marked failed and the run continues (5.6, 8.1)."""
    coordinator = _engine_coordinator(option_days=1)
    state = {"first": None}

    async def history(plant_id, start, end, *, measure_points, interval):
        if state["first"] is None:
            state["first"] = start
        if start == state["first"]:
            raise ValueError("transient boom")
        return {plant_id: [_row(c, start, 100.0) for c in measure_points]}

    coordinator.plants_service.async_get_historical_data.side_effect = history
    engine, _throttle, store = _make_engine(hass, coordinator)

    with patch("custom_components.sungrow.backfill.import_statistics"):
        summary = await engine.async_run()

    # The failing chunk is attempted BACKFILL_MAX_RETRIES + 1 times before it is abandoned.
    failing_calls = sum(
        1
        for call in coordinator.plants_service.async_get_historical_data.await_args_list
        if call.args[1] == state["first"]
    )
    assert failing_calls == BACKFILL_MAX_RETRIES + 1
    assert summary.failed_chunks == 1
    assert summary.completed is False
    # The remaining 7 chunks still imported (run continued past the failure).
    assert summary.imported_hours > 0
    engine._sleep.assert_awaited()  # transient retry delay slept

    marker = store.async_set_marker.await_args.args[1]
    assert marker["completed"] is False
    assert marker["partial"] is True
    assert marker["failed_chunks"] == 1


@pytest.mark.asyncio
async def test_engine_auth_error_stops_and_defers(hass: HomeAssistant):
    """An auth error stops the run immediately, records a partial marker, and re-raises (8.5)."""
    coordinator = _engine_coordinator(option_days=1)
    coordinator.plants_service.async_get_historical_data.side_effect = AuthError({"result_code": "E900"})
    engine, _throttle, store = _make_engine(hass, coordinator)

    with (
        patch("custom_components.sungrow.backfill.import_statistics") as mock_import,
        pytest.raises(AuthError),
    ):
        await engine.async_run()

    # Stopped on the very first call; nothing imported.
    assert coordinator.plants_service.async_get_historical_data.await_count == 1
    mock_import.assert_not_called()

    marker = store.async_set_marker.await_args.args[1]
    assert marker["completed"] is False
    assert marker["partial"] is True


# ---------------------------------------------------------------------------
# Task 13 - BackfillManager
# ---------------------------------------------------------------------------


class _FakeEntry:
    """A minimal config-entry stand-in for the manager (tracks background tasks)."""

    def __init__(self, coordinators: list, options: dict | None = None) -> None:
        self.entry_id = "backfill_test_entry"
        self.options = options or {}
        self.runtime_data = SimpleNamespace(coordinators=coordinators)
        self.background_tasks: list[asyncio.Task] = []

    def async_create_background_task(self, hass, coro, name):  # noqa: ANN001, ANN201
        task = asyncio.ensure_future(coro)
        self.background_tasks.append(task)
        return task


def _mgr_coordinator(plant_id: str, *, history=None, option_days: int | None = 1, plant_name: str | None = None):
    """A coordinator stand-in for the manager, with a config entry and a plants_service."""
    options: dict = {}
    if option_days is not None:
        options[CONF_BACKFILL_DAYS] = option_days
    service = SimpleNamespace(
        measure_points=dict(_MEASURE_POINTS),
        async_get_historical_data=AsyncMock(),
    )
    if history is not None:
        service.async_get_historical_data.side_effect = history
    else:
        service.async_get_historical_data.return_value = {}
    return SimpleNamespace(
        plant_id=plant_id,
        plant_name=plant_name or f"Plant {plant_id}",
        config_entry=SimpleNamespace(options=options),
        plants_service=service,
    )


def _make_manager(hass: HomeAssistant, coordinators: list, *, entry_options=None, markers=None):
    """Build a manager with a non-sleeping shared throttle and a fake marker store."""
    entry = _FakeEntry(coordinators, options=entry_options)
    manager = BackfillManager(hass, entry)
    fast = Throttle(min_interval=0.0)
    fast._sleep = AsyncMock()  # type: ignore[method-assign]
    manager._throttle = fast  # engines are created lazily, so they pick this up
    marker_map = markers or {}
    manager._store = SimpleNamespace(  # type: ignore[assignment]
        async_get_marker=AsyncMock(side_effect=lambda pid: marker_map.get(pid)),
        async_set_marker=AsyncMock(),
    )
    return manager, entry, manager._store


@pytest.mark.asyncio
async def test_manager_auto_start_gates_on_marker(hass: HomeAssistant):
    """A completed marker covering the default window skips that plant; others run (1.1, 1.4)."""
    now = dt_util.utcnow()
    covered = {
        "completed": True,
        "partial": False,
        "window_start": (now - timedelta(days=40)).isoformat(),
        "window_end": now.isoformat(),
        "last_run": now.isoformat(),
        "failed_chunks": 0,
    }
    c_done = _mgr_coordinator("done")
    c_new = _mgr_coordinator("new")
    manager, entry, _store = _make_manager(hass, [c_done, c_new], markers={"done": covered})

    with patch("custom_components.sungrow.backfill.import_statistics"):
        await manager.async_start_automatic()
        await asyncio.gather(*entry.background_tasks)

    # The plant whose completed marker covers the default window is not run again.
    c_done.plants_service.async_get_historical_data.assert_not_awaited()
    # The plant with no marker is backfilled.
    c_new.plants_service.async_get_historical_data.assert_awaited()


@pytest.mark.asyncio
async def test_manager_rejects_plant_already_running(hass: HomeAssistant):
    """A second run request for an in-flight plant is rejected, not started twice (2.4)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(plant_id, start, end, *, measure_points, interval):
        started.set()
        await release.wait()
        return {}

    coordinator = _mgr_coordinator("p1", history=slow)
    manager, entry, _store = _make_manager(hass, [coordinator])

    with patch("custom_components.sungrow.backfill.import_statistics"):
        await manager.async_run_on_demand(plant_ids=None, start_date=None)
        await started.wait()
        assert manager.is_running("p1") is True

        # Second request while the first is still running must be ignored.
        await manager.async_run_on_demand(plant_ids=["p1"], start_date=None)
        assert len(entry.background_tasks) == 1

        release.set()
        await asyncio.gather(*entry.background_tasks)

    assert manager.is_running("p1") is False


@pytest.mark.asyncio
async def test_manager_one_plant_failure_does_not_stop_others(hass: HomeAssistant):
    """One plant erroring out never stops the others' runs (9.1, 9.3)."""

    async def good(plant_id, start, end, *, measure_points, interval):
        return {plant_id: [_row(code, start, 100.0) for code in measure_points]}

    c_ok = _mgr_coordinator("ok", history=good)
    c_bad = _mgr_coordinator("bad", history=AuthError({"result_code": "E900"}))
    manager, entry, _store = _make_manager(hass, [c_ok, c_bad])

    with patch("custom_components.sungrow.backfill.import_statistics") as mock_import:
        await manager.async_run_on_demand(plant_ids=None, start_date=None)
        # gather must not raise even though the "bad" plant's run raised internally.
        await asyncio.gather(*entry.background_tasks)

    # The healthy plant completed and imported despite the other's failure.
    c_ok.plants_service.async_get_historical_data.assert_awaited()
    assert mock_import.called
    assert manager.is_running("ok") is False
    assert manager.is_running("bad") is False


@pytest.mark.asyncio
async def test_manager_engines_share_one_throttle(hass: HomeAssistant):
    """All engines of an entry share the manager's single throttle (9.4)."""
    c1 = _mgr_coordinator("p1")
    c2 = _mgr_coordinator("p2")
    manager, entry, _store = _make_manager(hass, [c1, c2])

    with patch("custom_components.sungrow.backfill.import_statistics"):
        await manager.async_run_on_demand(plant_ids=None, start_date=None)
        await asyncio.gather(*entry.background_tasks)

    assert manager._engines["p1"]._throttle is manager._engines["p2"]._throttle
    assert manager._engines["p1"]._throttle is manager._throttle


@pytest.mark.asyncio
async def test_manager_shutdown_cancels_tasks_leaving_imports_intact(hass: HomeAssistant):
    """Shutdown cancels in-flight runs; hours imported before cancellation stay intact (1.5)."""
    started = asyncio.Event()
    release = asyncio.Event()
    state = {"calls": 0}
    imported: list = []

    async def slow(plant_id, start, end, *, measure_points, interval):
        state["calls"] += 1
        if state["calls"] == 1:
            # First chunk returns rows so at least one import lands before we block.
            return {plant_id: [_row(code, start, 100.0) for code in measure_points]}
        started.set()
        await release.wait()  # block so shutdown cancels the run mid-flight
        return {}

    def record_import(hass_, target, data):
        imported.append((target.statistic_id, tuple(d["start"] for d in data)))

    coordinator = _mgr_coordinator("p1", history=slow)
    manager, entry, _store = _make_manager(hass, [coordinator])

    with patch("custom_components.sungrow.backfill.import_statistics", side_effect=record_import):
        await manager.async_run_on_demand(plant_ids=None, start_date=None)
        await started.wait()
        assert manager.is_running("p1") is True

        await manager.async_shutdown()  # cancels the blocked task

    # The task is gone and the first chunk's imports remain durable (never rolled back).
    assert manager.is_running("p1") is False
    assert manager._tasks == {}
    assert len(imported) >= 1


# ---------------------------------------------------------------------------
# Task 14 - setup wiring in __init__.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_setup_constructs_manager_and_starts_run(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """A cloud entry builds a BackfillManager and kicks off the automatic run (1.1)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.conftest import MOCK_CONFIG_DATA

    with patch.object(BackfillManager, "async_start_automatic", new=AsyncMock()) as mock_start:
        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert isinstance(entry.runtime_data.backfill, BackfillManager)
    mock_start.assert_awaited()


@pytest.mark.asyncio
async def test_modbus_only_setup_skips_backfill(hass: HomeAssistant):
    """A Modbus-only (cloud-free) entry never constructs a manager (1.2)."""
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.sungrow.const import (
        CONF_MODBUS_HOST,
        CONF_MODEL,
        CONF_SCAN_INTERVAL,
        CONF_SERIAL,
        CONF_TRANSPORT,
        TRANSPORT_MODBUS_ONLY,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "SN123",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "10.0.0.9",
        },
        options={CONF_SCAN_INTERVAL: 30},
        unique_id="modbus_SN123",
    )
    entry.add_to_hass(hass)

    client = MagicMock()
    client.async_read_realtime = AsyncMock(
        return_value={"grid_frequency": {"code": "grid_frequency", "value": 49.9, "unit": "Hz", "source": "modbus"}}
    )
    with patch("custom_components.sungrow.modbus.SungrowModbusClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Backfill is cloud-only: the Modbus-only path leaves the field as None.
    assert entry.runtime_data.backfill is None


@pytest.mark.asyncio
async def test_realtime_poll_not_blocked_by_parked_backfill():
    """An in-flight run parked on the throttle never blocks an independent poll (5.4, 5.5)."""
    throttle = Throttle(min_interval=0.0)
    parked = asyncio.Event()
    release = asyncio.Event()

    async def park(_delay: float) -> None:
        parked.set()
        await release.wait()

    throttle._sleep = park  # type: ignore[method-assign]

    # A backfill "run" parked on a throttle backoff.
    backfill_task = asyncio.ensure_future(throttle.backoff())
    await parked.wait()

    # An independent realtime poll (no shared lock with the throttle) runs to completion.
    poll_done = False

    async def realtime_poll() -> None:
        nonlocal poll_done
        await asyncio.sleep(0)
        poll_done = True

    await asyncio.wait_for(realtime_poll(), timeout=1.0)
    assert poll_done is True
    assert not backfill_task.done()  # backfill is still parked, poll went ahead

    release.set()
    await backfill_task


@pytest.mark.asyncio
async def test_unload_shuts_down_backfill_manager(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Unloading a cloud entry cancels in-flight runs via manager.async_shutdown (1.5)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.conftest import MOCK_CONFIG_DATA

    with patch.object(BackfillManager, "async_start_automatic", new=AsyncMock()):
        entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    manager = entry.runtime_data.backfill
    assert isinstance(manager, BackfillManager)

    with patch.object(manager, "async_shutdown", new=AsyncMock()) as mock_shutdown:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    mock_shutdown.assert_awaited()


# ---------------------------------------------------------------------------
# Task 15 - on-demand backfill service
# ---------------------------------------------------------------------------


async def _setup_cloud_entry(hass: HomeAssistant):
    """Set up a two-plant cloud entry with the automatic run stubbed out."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from tests.conftest import MOCK_CONFIG_DATA

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), unique_id="test_app_id")
    entry.add_to_hass(hass)
    with patch.object(BackfillManager, "async_start_automatic", new=AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.asyncio
async def test_backfill_service_is_registered(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Setting up a cloud entry registers the sungrow.backfill admin service (2.1)."""
    await _setup_cloud_entry(hass)
    assert hass.services.has_service(DOMAIN, "backfill") is True


@pytest.mark.asyncio
async def test_backfill_service_dispatches_to_all_plants(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """Calling the service with no config_entry dispatches a run to every plant (2.2)."""
    entry = await _setup_cloud_entry(hass)
    manager = entry.runtime_data.backfill

    with patch.object(manager, "_start_run") as mock_start_run:
        await hass.services.async_call(DOMAIN, "backfill", {}, blocking=True)

    started = {call.args[0].plant_id for call in mock_start_run.call_args_list}
    # MOCK_PLANT_LIST has two plants; both are dispatched.
    assert started == {"12345", "67890"}


@pytest.mark.asyncio
async def test_backfill_service_passes_start_date(hass: HomeAssistant, mock_setup_auth, mock_plants_service):
    """An explicit start_date is parsed to UTC midnight and passed through (2.3)."""
    entry = await _setup_cloud_entry(hass)
    manager = entry.runtime_data.backfill

    with patch.object(manager, "async_run_on_demand", new=AsyncMock()) as mock_run:
        await hass.services.async_call(DOMAIN, "backfill", {"start_date": "2024-01-15"}, blocking=True)

    mock_run.assert_awaited_once()
    kwargs = mock_run.await_args.kwargs
    assert kwargs["plant_ids"] is None
    assert kwargs["start_date"] == datetime(2024, 1, 15, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Task 16 - partial-failure Repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_partial_run_raises_repair_then_clears_it(hass: HomeAssistant):
    """A partial run raises the backfill_partial issue; a later clean run clears it (8.1, 8.2)."""
    from homeassistant.helpers import issue_registry as ir

    fail_state = {"first": None}

    async def partial_history(plant_id, start, end, *, measure_points, interval):
        if fail_state["first"] is None:
            fail_state["first"] = start
        if start == fail_state["first"]:
            raise ValueError("transient boom")  # this chunk never succeeds -> partial
        return {plant_id: [_row(code, start, 100.0) for code in measure_points]}

    coordinator = _mgr_coordinator("p1", history=partial_history)
    manager, entry, _store = _make_manager(hass, [coordinator])
    # Pre-create the engine so its transient-retry sleep is a no-op (keeps the test fast).
    manager._engine_for(coordinator)._sleep = AsyncMock()  # type: ignore[method-assign]

    with patch("custom_components.sungrow.backfill.import_statistics"):
        await manager.async_run_on_demand(plant_ids=None, start_date=None)
        await asyncio.gather(*entry.background_tasks)

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "backfill_partial_p1") is not None

    # A subsequent fully successful run clears the Repair.
    coordinator.plants_service.async_get_historical_data.side_effect = None
    coordinator.plants_service.async_get_historical_data.return_value = {}
    entry.background_tasks.clear()

    await manager.async_run_on_demand(plant_ids=None, start_date=None)
    await asyncio.gather(*entry.background_tasks)

    assert issue_reg.async_get_issue(DOMAIN, "backfill_partial_p1") is None
