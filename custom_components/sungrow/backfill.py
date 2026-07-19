"""Backfill historical iSolarCloud data into Home Assistant long-term statistics.

This module is split into a **pure-logic core** (window resolution, time chunking,
point batching, hourly aggregation, unit conversion) that carries no I/O and is
exercised by Hypothesis property tests, and an **I/O orchestration shell** (series
resolution, recorder import, throttle, engine run loop, manager) layered on top.

See ``.kiro/specs/backfill-historical-statistics`` for the design and requirements.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_CHUNK_WINDOW,
    BACKFILL_INTERVAL,
    BACKFILL_MAX_RETRIES,
    BACKFILL_MIN_CALL_INTERVAL,
    CONF_BACKFILL_DAYS,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    MAX_POINTS_PER_CALL,
)
from .coordinator import is_auth_error, is_rate_limit_error
from .energy_units import normalize_energy_point
from .measure_points import resolve_classification

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import SungrowPlantCoordinator

# Escalating rate-limit backoff is capped at one hour (mirrors the coordinator).
_MAX_BACKOFF_SECONDS = 3600.0

# Short delay between transient-error retries of the same (chunk, batch) call.
_TRANSIENT_RETRY_DELAY = 1.0

# The (device_class, state_class) pairs that mark a measure point as a Backfill_Point.
_ENERGY_CLASS = (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING)
_POWER_CLASS = (SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT)

# Default import units when a live entity does not supply one (source rows are Wh->kWh
# for energy and W for power after normalisation).
_DEFAULT_UNIT: dict[str, str] = {"energy": "kWh", "power": "W"}

_LOGGER = logging.getLogger(__name__)

# Storage schema version for the per-entry Backfill marker Store.
STORAGE_VERSION = 1

# Translation key / Repair issue id prefix raised when a run finishes with failed chunks.
BACKFILL_PARTIAL_ISSUE = "backfill_partial"

# Shared troubleshooting doc surfaced on the partial-failure Repair (mirrors the coordinator).
_BACKFILL_LEARN_MORE = "https://github.com/KRoperUK/sungrow-hass/blob/main/docs/TROUBLESHOOTING.md"


class InvalidRangeError(Exception):
    """Raised when a requested Backfill range is invalid (e.g. start after now)."""


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MinuteRow:
    """A single minute-level reading parsed from the historical endpoint.

    ``value`` is normalised to the target unit (Wh -> kWh for energy) before use.
    """

    timestamp: datetime  # UTC
    value: float


@dataclass(frozen=True)
class HistoryWindow:
    """The bounded time range a Backfill run imports. Both bounds are UTC."""

    start: datetime
    end: datetime  # == now


@dataclass(frozen=True)
class SeriesTarget:
    """The resolved import target for one Backfill_Point."""

    point_code: str
    statistic_id: str
    unit: str | None
    kind: Literal["energy", "power"]
    is_external: bool
    metadata: StatisticMetaData


@dataclass(frozen=True)
class RunSummary:
    """The outcome of one Backfill run for one plant."""

    plant_id: str
    window: HistoryWindow
    imported_hours: int
    skipped_empty_ranges: int
    failed_chunks: int
    completed: bool  # True when no failed chunks


# ---------------------------------------------------------------------------
# Marker persistence
# ---------------------------------------------------------------------------


class BackfillStore:
    """Persist per-plant Backfill completion markers for a config entry.

    Wraps a HA ``Store[dict]`` keyed by ``f"{DOMAIN}.backfill_state_{entry_id}"``.
    The stored document maps ``plant_id`` -> marker dict with ``completed``,
    ``partial``, ``window_start``, ``window_end``, ``last_run`` and ``failed_chunks``.
    """

    def __init__(self, hass: Any, entry: ConfigEntry) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.backfill_state_{entry.entry_id}",
        )
        self._data: dict[str, Any] | None = None

    async def _async_load(self) -> dict[str, Any]:
        if self._data is None:
            self._data = await self._store.async_load() or {"plants": {}}
            self._data.setdefault("plants", {})
        return self._data

    async def async_get_marker(self, plant_id: str) -> dict[str, Any] | None:
        """Return the persisted marker for *plant_id*, or None if absent."""
        data = await self._async_load()
        result: dict[str, Any] | None = data["plants"].get(plant_id)
        return result

    async def async_set_marker(self, plant_id: str, marker: dict[str, Any]) -> None:
        """Persist *marker* for *plant_id* and flush to disk."""
        data = await self._async_load()
        data["plants"][plant_id] = marker
        await self._store.async_save(data)


# ---------------------------------------------------------------------------
# Pure core: window resolution
# ---------------------------------------------------------------------------


def resolve_window(
    *,
    now: datetime,
    option_days: int | None,
    start_override: datetime | None,
) -> HistoryWindow:
    """Resolve the bounded History_Window for a run.

    The window always ends at ``now``. Its length defaults to
    ``DEFAULT_BACKFILL_DAYS`` and uses ``option_days`` when provided, clamped to
    ``[1, MAX_BACKFILL_DAYS]`` days (a clamp is logged). A ``start_override`` sets
    the start explicitly, still clamped so the window never exceeds the maximum
    length. A ``start_override`` later than ``now`` raises ``InvalidRangeError``.

    Requirements: 2.3, 3.1, 3.2, 3.3, 3.4, 3.5.
    """
    if start_override is not None and start_override > now:
        raise InvalidRangeError(f"start_override {start_override.isoformat()} is later than now {now.isoformat()}")

    requested_days = DEFAULT_BACKFILL_DAYS if option_days is None else option_days
    clamped_days = max(1, min(requested_days, MAX_BACKFILL_DAYS))
    if clamped_days != requested_days:
        _LOGGER.info(
            "Backfill window length %s days clamped to %s days (allowed range 1-%s)",
            requested_days,
            clamped_days,
            MAX_BACKFILL_DAYS,
        )

    max_start = now - timedelta(days=1)  # window is always >= 1 day
    default_start = now - timedelta(days=clamped_days)
    earliest_start = now - timedelta(days=MAX_BACKFILL_DAYS)

    # Honor an explicit start, but never exceed the maximum window length and never
    # produce a zero/negative-length window; otherwise use the (clamped) default.
    start = min(max(start_override, earliest_start), max_start) if start_override is not None else default_start

    return HistoryWindow(start=start, end=now)


# ---------------------------------------------------------------------------
# Pure core: chunking and batching
# ---------------------------------------------------------------------------


def chunk_time_window(window: HistoryWindow, chunk: timedelta) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into consecutive, non-overlapping sub-ranges.

    Each sub-range spans at most *chunk*; the ranges are returned in ascending
    chronological order and together cover exactly ``[start, end)``.

    Requirements: 4.2, 4.3, 4.4.
    """
    if chunk <= timedelta(0):
        raise ValueError("chunk size must be positive")

    chunks: list[tuple[datetime, datetime]] = []
    cursor = window.start
    while cursor < window.end:
        nxt = min(cursor + chunk, window.end)
        chunks.append((cursor, nxt))
        cursor = nxt
    return chunks


def batch_points(points: list[SeriesTarget], max_size: int = MAX_POINTS_PER_CALL) -> list[list[SeriesTarget]]:
    """Partition *points* into batches of at most *max_size*, preserving order.

    Requirements: 4.1.
    """
    if max_size <= 0:
        raise ValueError("max_size must be positive")
    return [points[i : i + max_size] for i in range(0, len(points), max_size)]


# ---------------------------------------------------------------------------
# Pure core: unit conversion
# ---------------------------------------------------------------------------


def normalize_row_value(value: Any, unit: str | None) -> dict[str, Any]:
    """Normalise one ``(value, unit)`` pair via ``energy_units.normalize_energy_point``.

    Returns the normalised point dict (Wh -> kWh for energy; unchanged otherwise).

    Requirements: 7.5, 7.6.
    """
    return normalize_energy_point({"value": value, "unit": unit})


# ---------------------------------------------------------------------------
# Pure core: hourly aggregation
# ---------------------------------------------------------------------------


def _floor_to_hour(ts: datetime) -> datetime:
    """Floor *ts* to the start of its UTC hour."""
    return dt_util.as_utc(ts).replace(minute=0, second=0, microsecond=0)


def build_hourly_statistics(
    rows: list[MinuteRow],
    kind: Literal["energy", "power"],
    *,
    running_sum: float = 0.0,
    prev_value: float | None = None,
) -> tuple[list[StatisticData], float]:
    """Aggregate minute rows for ONE series into hourly ``StatisticData``.

    Energy: per hour ``state`` is the last cumulative reading in that hour, and
    ``sum`` is a running total that is non-decreasing across the window (negative
    minute-to-minute deltas are clamped to zero to guard meter resets/dips). The
    updated running sum is returned so it can be carried across chunks. ``prev_value``
    is the last reading of the previous chunk (``None`` for the first chunk); passing
    it lets per-chunk aggregation compose to the same result as aggregating the whole
    window at once, since the delta spanning a chunk boundary is not lost.

    Power: per hour ``mean``/``min``/``max`` over that hour's samples.

    Every ``StatisticData.start`` is aligned to an exact UTC hour boundary, and at
    most one entry is produced per hour. Deterministic for equal input.

    Requirements: 4.5, 6.3, 7.1, 7.2.
    """
    if kind == "energy":
        return _build_energy(rows, running_sum, prev_value)
    return _build_power(rows), running_sum


def _build_energy(
    rows: list[MinuteRow], running_sum: float, prev_value: float | None
) -> tuple[list[StatisticData], float]:
    # Sort chronologically so cumulative deltas and per-hour "last value" are stable.
    ordered = sorted(rows, key=lambda r: r.timestamp)

    running = running_sum
    prev = prev_value
    # Preserve first-seen hour order (already ascending after the sort).
    per_hour: dict[datetime, dict[str, float]] = {}

    for row in ordered:
        hour = _floor_to_hour(row.timestamp)
        if prev is None:
            delta = 0.0
        else:
            delta = row.value - prev
            if delta < 0:
                # Meter reset or spurious dip: clamp so the running sum never drops.
                delta = 0.0
        running += delta
        prev = row.value
        # Last write wins -> per-hour state is the last cumulative reading in the hour,
        # and sum is the running total at the end of the hour.
        per_hour[hour] = {"state": row.value, "sum": running}

    result: list[StatisticData] = [
        {"start": hour, "state": vals["state"], "sum": vals["sum"]} for hour, vals in per_hour.items()
    ]
    return result, running


def _build_power(rows: list[MinuteRow]) -> list[StatisticData]:
    ordered = sorted(rows, key=lambda r: r.timestamp)

    buckets: dict[datetime, list[float]] = {}
    for row in ordered:
        buckets.setdefault(_floor_to_hour(row.timestamp), []).append(row.value)

    result: list[StatisticData] = []
    for hour, values in buckets.items():
        result.append(
            {
                "start": hour,
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Series resolution and Statistic_Id scoping (Task 7)
# ---------------------------------------------------------------------------


def select_backfill_points(measure_points: dict[str, str]) -> list[tuple[str, str]]:
    """Select the cumulative-energy and power Backfill_Points from a measure-point map.

    ``measure_points`` maps ``point_id`` -> ``code`` (the ``plants_service.measure_points``
    catalogue). Each point is classified with the same ``resolve_classification`` the sensor
    platform uses; a point is a Backfill_Point when it is cumulative energy
    (``ENERGY``/``TOTAL_INCREASING`` -> ``kind="energy"``) or power
    (``POWER``/``MEASUREMENT`` -> ``kind="power"``). Returns ``(code, kind)`` pairs in the
    catalogue's iteration order so backfilled series line up with the live sensors.

    Requirements: 7.1, 7.2.
    """
    selected: list[tuple[str, str]] = []
    for point_id, code in measure_points.items():
        classification = resolve_classification(None, code, point_id)
        if classification == _ENERGY_CLASS:
            selected.append((code, "energy"))
        elif classification == _POWER_CLASS:
            selected.append((code, "power"))
    return selected


def build_series_target(
    *,
    plant_id: str,
    point_code: str,
    kind: Literal["energy", "power"],
    entity_id: str | None,
    unit: str | None,
) -> SeriesTarget:
    """Derive the :class:`SeriesTarget` (Statistic_Id, source, metadata) for one point.

    A live entity (``entity_id`` set) imports into that entity's series
    (``statistic_id = entity_id``, ``source="recorder"``, ``is_external=False``); otherwise
    the point imports into a per-plant external series
    (``statistic_id = f"sungrow:{plant_id}_{point_code}"``, ``source=DOMAIN``,
    ``is_external=True``). Scoping the external id by ``plant_id`` guarantees no cross-plant
    collision. ``has_mean``/``has_sum`` follow ``kind`` (energy -> sum, power -> mean) so the
    recorder accepts the import, and the unit falls back to the kind default when the live
    entity supplies none.

    Requirements: 7.1, 7.2, 7.3, 7.4, 9.2.
    """
    if entity_id is not None:
        statistic_id = entity_id
        source = "recorder"
        is_external = False
    else:
        statistic_id = f"sungrow:{plant_id}_{point_code}"
        source = DOMAIN
        is_external = True

    # ``mean_type`` replaces the deprecated ``has_mean`` bool from HA 2026.11 onwards:
    # power series need an ARITHMETIC mean over each hour, energy series don't need a
    # mean at all (they use ``has_sum``). Both fields are set for backward compatibility
    # with older HA that still consults ``has_mean``.
    from homeassistant.components.recorder.models.statistics import StatisticMeanType

    mean_type = StatisticMeanType.ARITHMETIC if kind == "power" else StatisticMeanType.NONE

    resolved_unit = unit if unit else _DEFAULT_UNIT[kind]
    metadata: StatisticMetaData = {  # type: ignore[typeddict-item]
        "has_mean": kind == "power",
        "mean_type": mean_type,
        "has_sum": kind == "energy",
        "name": None,
        "source": source,
        "statistic_id": statistic_id,
        "unit_of_measurement": resolved_unit,
    }
    return SeriesTarget(
        point_code=point_code,
        statistic_id=statistic_id,
        unit=resolved_unit,
        kind=kind,
        is_external=is_external,
        metadata=metadata,
    )


def _live_unit(hass: HomeAssistant, registry: er.EntityRegistry, entity_id: str) -> str | None:
    """Return the live entity's unit from its current state, else its registry entry."""
    state = hass.states.get(entity_id)
    if state is not None:
        unit = state.attributes.get("unit_of_measurement")
        if unit:
            return str(unit)
    entry = registry.async_get(entity_id)
    if entry is not None and entry.unit_of_measurement:
        return str(entry.unit_of_measurement)
    return None


async def async_resolve_series(hass: HomeAssistant, coordinator: SungrowPlantCoordinator) -> list[SeriesTarget]:
    """Map each Backfill_Point for the coordinator's plant to its :class:`SeriesTarget`.

    Selects the cumulative-energy and power points from
    ``coordinator.plants_service.measure_points`` and, for each, looks up the live sensor by
    ``unique_id`` ``f"{plant_id}_{point_code}"`` in the entity registry. A found entity yields
    a live-entity series (unit taken from the live entity); an absent one falls back to a
    per-plant external series.

    Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 9.2.
    """
    registry = er.async_get(hass)
    plant_id = coordinator.plant_id
    assert coordinator.plants_service is not None  # Backfill is cloud-only; never called for Modbus entries
    measure_points = coordinator.plants_service.measure_points

    targets: list[SeriesTarget] = []
    for point_code, kind in select_backfill_points(measure_points):
        unique_id = f"{plant_id}_{point_code}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        unit = _live_unit(hass, registry, entity_id) if entity_id is not None else None
        targets.append(
            build_series_target(
                plant_id=plant_id,
                point_code=point_code,
                kind=kind,  # type: ignore[arg-type]
                entity_id=entity_id,
                unit=unit,
            )
        )
    return targets


# ---------------------------------------------------------------------------
# Idempotent import router (Task 8)
# ---------------------------------------------------------------------------


def import_statistics(hass: HomeAssistant, target: SeriesTarget, data: list[StatisticData]) -> None:
    """Import hourly statistics for one series through the recorder helpers.

    External series route to ``async_add_external_statistics`` and live-entity series to
    ``async_import_statistics``; both overwrite by ``(statistic_id, start_hour)``, so
    re-importing the same or a retried chunk is a clean overwrite rather than a duplicate
    (idempotent, hour-local). An empty ``data`` list is a no-op.

    Requirements: 6.1, 6.2, 6.4, 7.3, 7.4.
    """
    if not data:
        return

    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        async_import_statistics,
    )

    if target.is_external:
        async_add_external_statistics(hass, target.metadata, data)
    else:
        async_import_statistics(hass, target.metadata, data)


# ---------------------------------------------------------------------------
# Shared Throttle (Task 9)
# ---------------------------------------------------------------------------


class Throttle:
    """Serialise iSolarCloud calls across a config entry's Backfill engines.

    ``acquire`` waits until at least ``min_interval`` seconds have elapsed since the previous
    acquire, so a single shared instance keeps the combined call rate across concurrently
    backfilling plants under the quota (Requirements 5.1, 9.4). ``backoff`` sleeps an
    escalating delay after a rate-limit rejection (doubling each call, capped at one hour;
    Requirement 5.2) and ``reset_backoff`` clears the escalation once calls succeed again.
    """

    def __init__(self, min_interval: float = BACKFILL_MIN_CALL_INTERVAL, hass: Any = None) -> None:
        self._min_interval = min_interval
        self._hass = hass
        self._lock = asyncio.Lock()
        self._last: float | None = None
        self._backoff: float = 0.0

    def _monotonic(self) -> float:
        return time.monotonic()

    async def _sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)

    async def acquire(self) -> None:
        """Await until ``min_interval`` has elapsed since the previous acquire."""
        async with self._lock:
            now = self._monotonic()
            if self._last is not None:
                wait = self._min_interval - (now - self._last)
                if wait > 0:
                    await self._sleep(wait)
                    now = self._monotonic()
            self._last = now

    async def backoff(self) -> None:
        """Sleep an escalating (doubling) delay, capped at one hour."""
        if self._backoff <= 0:
            self._backoff = max(self._min_interval, 1.0)
        else:
            self._backoff = min(self._backoff * 2, _MAX_BACKOFF_SECONDS)
        await self._sleep(self._backoff)

    def reset_backoff(self) -> None:
        """Clear the escalating backoff after a successful call."""
        self._backoff = 0.0


# ---------------------------------------------------------------------------
# Error classification (Task 10)
# ---------------------------------------------------------------------------

ErrorClass = Literal["auth", "rate_limit", "transient"]


def classify_error(err: Exception) -> ErrorClass:
    """Classify an ``async_get_historical_data`` failure for the run loop.

    Reuses the coordinator's ``is_auth_error`` and ``is_rate_limit_error`` (E998/E999)
    helpers so the Backfill path reacts to failures exactly like the realtime poll: auth
    errors stop the run and defer to reauth, rate-limit errors trigger a throttle backoff,
    and anything else is a transient error eligible for a bounded retry.

    Requirements: 5.2, 5.6, 8.5.
    """
    if is_auth_error(err):
        return "auth"
    if is_rate_limit_error(err):
        return "rate_limit"
    return "transient"


# ---------------------------------------------------------------------------
# Engine run loop (Task 11)
# ---------------------------------------------------------------------------


def _coerce_value(raw: Any, unit: str | None, kind: Literal["energy", "power"]) -> float | None:
    """Normalise and coerce one source reading to a float, or ``None`` to drop it.

    Energy rows are normalised (Wh -> kWh) via ``normalize_row_value`` before coercion;
    power rows are used as-is. Rows whose value is ``None``/``""``/non-numeric are dropped
    by returning ``None``.
    """
    value: Any = raw
    if kind == "energy":
        value = normalize_row_value(raw, unit)["value"]
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BackfillEngine:
    """Orchestrate exactly one Backfill run for a single plant.

    A run resolves the bounded History_Window and the plant's Backfill series, then iterates
    Time_Chunks (ascending) x Point_Batches (<= 50), issuing throttled
    ``async_get_historical_data`` calls with explicit ``start_time``/``end_time`` and a
    5-minute ``interval``. Returned minute rows are aggregated into hourly ``StatisticData``
    (carrying the running ``sum`` and previous cumulative reading per series across chunks so
    energy sums compose correctly) and imported idempotently through the recorder helpers.

    Failures are classified: auth errors stop the run and defer to the integration's reauth
    handling; rate-limit errors trigger a throttle backoff and resume from the same
    (chunk, batch) via a progress cursor; transient errors retry the same call up to
    ``BACKFILL_MAX_RETRIES`` before the chunk is marked failed and the run continues. Empty
    ranges are skipped. On completion the run persists a marker and returns a ``RunSummary``.

    Requirements: 1.3, 4.3, 4.4, 5.2, 5.3, 5.6, 6.4, 8.1, 8.3, 8.4, 8.6.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: SungrowPlantCoordinator,
        throttle: Throttle,
        store: BackfillStore,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._throttle = throttle
        self._store = store

    async def _sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)

    async def async_run(self, *, start_date: datetime | None = None) -> RunSummary:
        """Execute one full Backfill run for this plant and return its :class:`RunSummary`."""
        plant_id = self._coordinator.plant_id
        entry = self._coordinator.config_entry
        option_days = entry.options.get(CONF_BACKFILL_DAYS) if entry is not None else None

        now = dt_util.utcnow()
        window = resolve_window(now=now, option_days=option_days, start_override=start_date)
        targets = await async_resolve_series(self._hass, self._coordinator)
        batches = batch_points(targets, MAX_POINTS_PER_CALL)
        chunks = chunk_time_window(window, BACKFILL_CHUNK_WINDOW)

        _LOGGER.info(
            "Backfill run starting for plant %s: window %s -> %s, %d series in %d batches, %d chunks",
            plant_id,
            window.start.isoformat(),
            window.end.isoformat(),
            len(targets),
            len(batches),
            len(chunks),
        )

        # Per-series carry-over so cumulative energy sums compose across ascending chunks.
        running_sums: dict[str, float] = {}
        prev_values: dict[str, float | None] = {}
        imported: set[tuple[str, datetime]] = set()
        skipped_empty_ranges = 0
        failed_chunks = 0

        # Flat work list so a rate-limit backoff can resume from the exact cursor position.
        work: list[tuple[tuple[datetime, datetime], list[SeriesTarget]]] = [
            (chunk, batch) for chunk in chunks for batch in batches
        ]

        cursor = 0
        transient_retries = 0
        try:
            while cursor < len(work):
                (start, end), batch = work[cursor]
                await self._throttle.acquire()
                try:
                    assert self._coordinator.plants_service is not None
                    raw = await self._coordinator.plants_service.async_get_historical_data(
                        plant_id,
                        start,
                        end,
                        measure_points=[t.point_code for t in batch],
                        interval=BACKFILL_INTERVAL,
                    )
                except asyncio.CancelledError:
                    # Cancelled between imports: already-imported hours stay durable. Propagate.
                    raise
                except Exception as err:  # noqa: BLE001 - classified below
                    error_class = classify_error(err)
                    if error_class == "auth":
                        _LOGGER.warning(
                            "Backfill run for plant %s stopped on authentication error; deferring to reauth: %s",
                            plant_id,
                            err,
                        )
                        await self._persist_marker(plant_id, window, now, failed_chunks, completed=False)
                        raise
                    if error_class == "rate_limit":
                        _LOGGER.debug(
                            "Backfill rate-limited on chunk %s-%s (plant %s); backing off",
                            start.isoformat(),
                            end.isoformat(),
                            plant_id,
                        )
                        await self._throttle.backoff()
                        continue  # resume the SAME (chunk, batch) without advancing
                    # Transient: retry the same call a bounded number of times.
                    transient_retries += 1
                    if transient_retries <= BACKFILL_MAX_RETRIES:
                        _LOGGER.debug(
                            "Backfill transient error on chunk %s-%s (plant %s), retry %d/%d: %s",
                            start.isoformat(),
                            end.isoformat(),
                            plant_id,
                            transient_retries,
                            BACKFILL_MAX_RETRIES,
                            err,
                        )
                        await self._sleep(_TRANSIENT_RETRY_DELAY)
                        continue
                    failed_chunks += 1
                    _LOGGER.warning(
                        "Backfill chunk %s-%s (plant %s) failed after %d retries: %s",
                        start.isoformat(),
                        end.isoformat(),
                        plant_id,
                        BACKFILL_MAX_RETRIES,
                        err,
                    )
                    transient_retries = 0
                    cursor += 1
                    continue

                # Success: clear escalation and process the returned rows.
                self._throttle.reset_backoff()
                transient_retries = 0

                rows = raw.get(plant_id) or raw.get(str(plant_id)) or []
                if not rows:
                    skipped_empty_ranges += 1
                    _LOGGER.debug(
                        "Backfill chunk %s-%s (plant %s) returned no rows; skipping",
                        start.isoformat(),
                        end.isoformat(),
                        plant_id,
                    )
                    cursor += 1
                    continue

                self._aggregate_batch(rows, batch, running_sums, prev_values, imported)
                _LOGGER.debug(
                    "Backfill imported chunk %s-%s (plant %s): %d hourly rows so far",
                    start.isoformat(),
                    end.isoformat(),
                    plant_id,
                    len(imported),
                )
                cursor += 1
        except asyncio.CancelledError:
            _LOGGER.debug("Backfill run for plant %s cancelled", plant_id)
            raise

        completed = failed_chunks == 0
        await self._persist_marker(plant_id, window, now, failed_chunks, completed=completed)

        summary = RunSummary(
            plant_id=plant_id,
            window=window,
            imported_hours=len(imported),
            skipped_empty_ranges=skipped_empty_ranges,
            failed_chunks=failed_chunks,
            completed=completed,
        )
        _LOGGER.info(
            "Backfill run finished for plant %s: %d hours imported, %d empty ranges, %d failed chunks, completed=%s",
            plant_id,
            summary.imported_hours,
            summary.skipped_empty_ranges,
            summary.failed_chunks,
            summary.completed,
        )
        return summary

    def _aggregate_batch(
        self,
        rows: list[dict[str, Any]],
        batch: list[SeriesTarget],
        running_sums: dict[str, float],
        prev_values: dict[str, float | None],
        imported: set[tuple[str, datetime]],
    ) -> None:
        """Aggregate and import one chunk's rows for every series in *batch*."""
        rows_by_code: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            code = row.get("code") or ""
            rows_by_code.setdefault(code, []).append(row)

        for target in batch:
            minute_rows: list[MinuteRow] = []
            for row in rows_by_code.get(target.point_code, []):
                value = _coerce_value(row.get("value"), row.get("unit"), target.kind)
                if value is None:
                    continue
                minute_rows.append(MinuteRow(timestamp=row["timestamp"], value=value))

            if not minute_rows:
                continue

            sid = target.statistic_id
            stats, new_sum = build_hourly_statistics(
                minute_rows,
                target.kind,
                running_sum=running_sums.get(sid, 0.0),
                prev_value=prev_values.get(sid),
            )
            running_sums[sid] = new_sum
            # Carry the last cumulative reading so the next chunk's first delta is correct.
            prev_values[sid] = max(minute_rows, key=lambda r: r.timestamp).value

            import_statistics(self._hass, target, stats)
            for entry in stats:
                imported.add((sid, entry["start"]))

    async def _persist_marker(
        self,
        plant_id: str,
        window: HistoryWindow,
        last_run: datetime,
        failed_chunks: int,
        *,
        completed: bool,
    ) -> None:
        """Persist the per-plant completion marker for this run."""
        await self._store.async_set_marker(
            plant_id,
            {
                "completed": completed,
                "partial": failed_chunks > 0 or not completed,
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
                "last_run": last_run.isoformat(),
                "failed_chunks": failed_chunks,
            },
        )


# ---------------------------------------------------------------------------
# BackfillManager (Task 13)
# ---------------------------------------------------------------------------


class BackfillManager:
    """Own one :class:`BackfillEngine` per coordinator for a config entry.

    All engines of an entry share a single :class:`Throttle` (so the combined call rate
    across concurrently backfilling plants stays under the quota, Requirement 9.4) and a
    single :class:`BackfillStore` for the per-plant completion markers. The manager starts
    automatic runs after setup (gated on the persisted marker, Requirements 1.1, 1.4),
    dispatches on-demand service calls (Requirements 2.2, 2.3, 2.4), and tracks every run as
    a background task so unload can cancel them (Requirement 1.5).

    Each per-plant run is wrapped so one plant's failure never stops the others
    (Requirements 9.1, 9.3). On completion the manager logs the :class:`RunSummary` and
    raises or clears the partial-failure Repair.

    Requirements: 1.1, 1.4, 1.5, 2.2, 2.3, 2.4, 9.1, 9.3, 9.4.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        # One shared throttle and one shared marker store across all engines of this entry.
        self._throttle = Throttle(min_interval=BACKFILL_MIN_CALL_INTERVAL, hass=hass)
        self._store = BackfillStore(hass, entry)
        self._engines: dict[str, BackfillEngine] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # -- engine lifecycle ---------------------------------------------------

    def _engine_for(self, coordinator: SungrowPlantCoordinator) -> BackfillEngine:
        """Return (lazily creating) the engine for *coordinator*'s plant."""
        plant_id = coordinator.plant_id
        engine = self._engines.get(plant_id)
        if engine is None:
            engine = BackfillEngine(self._hass, coordinator, self._throttle, self._store)
            self._engines[plant_id] = engine
        return engine

    def _plant_name(self, plant_id: str) -> str:
        """Best-effort human name for *plant_id* (falls back to the id itself)."""
        for coordinator in self._entry.runtime_data.coordinators:
            if coordinator.plant_id == plant_id:
                return getattr(coordinator, "plant_name", plant_id)
        return plant_id

    # -- public API ---------------------------------------------------------

    def is_running(self, plant_id: str) -> bool:
        """Return whether a run is currently in flight for *plant_id* (Requirement 2.4)."""
        task = self._tasks.get(plant_id)
        return task is not None and not task.done()

    async def async_start_automatic(self) -> None:
        """Start an automatic run for each plant lacking a completed default-window marker.

        For each coordinator the persisted marker is read; if a completed marker already
        covers the default window, that plant is skipped (Requirements 1.1, 1.4). Otherwise a
        run covering the default window is started.
        """
        now = dt_util.utcnow()
        option_days = self._entry.options.get(CONF_BACKFILL_DAYS)
        default_window = resolve_window(now=now, option_days=option_days, start_override=None)

        for coordinator in self._entry.runtime_data.coordinators:
            plant_id = coordinator.plant_id
            marker = await self._store.async_get_marker(plant_id)
            if _marker_covers_window(marker, default_window):
                _LOGGER.debug(
                    "Backfill skipping automatic run for plant %s: completed marker already covers the default window",
                    plant_id,
                )
                continue
            self._start_run(coordinator)

    async def async_run_on_demand(self, *, plant_ids: list[str] | None, start_date: datetime | None) -> None:
        """Start on-demand runs for the addressed coordinators (all if *plant_ids* is None).

        A plant already running is rejected and logged rather than starting a second run
        (Requirements 2.2, 2.3, 2.4).
        """
        coordinators = list(self._entry.runtime_data.coordinators)
        if plant_ids is not None:
            wanted = set(plant_ids)
            coordinators = [c for c in coordinators if c.plant_id in wanted]

        for coordinator in coordinators:
            self._start_run(coordinator, start_date=start_date)

    async def async_shutdown(self) -> None:
        """Cancel and await every in-flight run task (Requirement 1.5).

        Cancellation between imports is safe: already-imported hours are durable and are
        re-imported identically by a later run, so no statistics are corrupted.
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # -- internals ----------------------------------------------------------

    def _start_run(self, coordinator: SungrowPlantCoordinator, *, start_date: datetime | None = None) -> bool:
        """Start a background run task for *coordinator*, unless one is already running."""
        plant_id = coordinator.plant_id
        if self.is_running(plant_id):
            _LOGGER.warning("Backfill already running for plant %s; ignoring new run request", plant_id)
            return False
        engine = self._engine_for(coordinator)
        task = self._entry.async_create_background_task(
            self._hass,
            self._run_engine(engine, plant_id, start_date=start_date),
            name=f"sungrow-backfill-{plant_id}",
        )
        self._tasks[plant_id] = task
        return True

    async def _run_engine(self, engine: BackfillEngine, plant_id: str, *, start_date: datetime | None) -> None:
        """Run one engine, isolating its failure and applying the summary's Repair state.

        Wrapping each run in its own task with its own try/except means one plant's error
        (or cancellation) never stops the others (Requirements 9.1, 9.3).
        """
        try:
            summary = await engine.async_run(start_date=start_date)
        except asyncio.CancelledError:
            _LOGGER.debug("Backfill run for plant %s cancelled", plant_id)
            raise
        except Exception:  # noqa: BLE001 - one plant's failure must not stop the others
            _LOGGER.exception("Backfill run for plant %s failed", plant_id)
        else:
            _LOGGER.info(
                "Backfill run summary for plant %s: %d hours imported, %d empty ranges, %d failed chunks, completed=%s",
                plant_id,
                summary.imported_hours,
                summary.skipped_empty_ranges,
                summary.failed_chunks,
                summary.completed,
            )
            self._apply_repair(summary)
        finally:
            self._tasks.pop(plant_id, None)

    def _apply_repair(self, summary: RunSummary) -> None:
        """Raise the partial-failure Repair on a partial run, clear it on a clean run.

        Requirements: 8.1, 8.2.
        """
        issue_id = f"{BACKFILL_PARTIAL_ISSUE}_{summary.plant_id}"
        if summary.completed:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=BACKFILL_PARTIAL_ISSUE,
            translation_placeholders={
                "plant": self._plant_name(summary.plant_id),
                "failed_chunks": str(summary.failed_chunks),
            },
            learn_more_url=_BACKFILL_LEARN_MORE,
        )


def _marker_covers_window(marker: dict[str, Any] | None, window: HistoryWindow) -> bool:
    """Return whether a completed marker already covers *window*'s start.

    A plant is gated from an automatic re-run only when its persisted marker is
    ``completed`` and its recorded window began at or before the default window's start,
    i.e. the earlier run already imported at least as far back as the default window asks
    for (Requirements 1.1, 1.4).
    """
    if not marker or not marker.get("completed"):
        return False
    window_start = marker.get("window_start")
    if not window_start:
        return False
    parsed = dt_util.parse_datetime(window_start)
    return parsed is not None and parsed <= window.start
