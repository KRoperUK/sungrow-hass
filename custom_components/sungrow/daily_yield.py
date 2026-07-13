"""Derive calendar-day yield from lifetime ``total_yield`` (#223 / Modbus SG-RS).

On several SG-RS + WiNet-S firmwares the documented "Daily power yields" register
(wire 5002) never resets at midnight — it climbs in lockstep with lifetime energy
and reports a multi-day cumulative value. Lifetime ``total_yield`` (wire 5003/5004)
matches the cloud, so "energy today" is computed as:

    daily = total_yield − total_yield_at_start_of_local_day

The baseline is the last ``total_yield`` observed on the previous local calendar
day (approximately end-of-yesterday / start-of-today). State is meant to be
persisted across restarts by the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class DailyYieldBaseline:
    """Mutable baseline used to derive today's yield from lifetime total."""

    # total_yield at the start of ``baseline_date`` (local calendar day).
    baseline: float | None = None
    baseline_date: date | None = None
    # Most recent total_yield sample (used as the next day's baseline on rollover).
    last_total: float | None = None

    def to_store(self) -> dict[str, Any]:
        """Serialize for HA Store."""
        return {
            "baseline": self.baseline,
            "baseline_date": self.baseline_date.isoformat() if self.baseline_date else None,
            "last_total": self.last_total,
        }

    @classmethod
    def from_store(cls, data: dict[str, Any] | None) -> DailyYieldBaseline:
        """Restore from HA Store (tolerant of missing/partial payloads)."""
        if not data:
            return cls()
        raw_date = data.get("baseline_date")
        try:
            baseline_date = date.fromisoformat(str(raw_date)) if raw_date else None
        except ValueError:
            baseline_date = None
        baseline = _as_float(data.get("baseline"))
        last_total = _as_float(data.get("last_total"))
        return cls(baseline=baseline, baseline_date=baseline_date, last_total=last_total)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def step_daily_yield(
    total_yield: float,
    local_date: date,
    state: DailyYieldBaseline,
) -> tuple[float, DailyYieldBaseline]:
    """Advance baseline state for one ``total_yield`` sample; return (daily, new_state).

    * On the first sample of a new local calendar day, the baseline becomes the
      previous sample's total (``last_total``), which is the best available estimate
      of energy at local midnight when polls run through the night.
    * On the first sample ever (no history), baseline is set to the current total so
      daily starts at 0 until the next midnight (midday install / empty store).
    * If total drops below the baseline (meter reset / firmware glitch), the baseline
      resets to the new total and daily is 0.
    """
    if state.baseline_date != local_date:
        # Prefer yesterday's last sample as start-of-today; else anchor at current total
        # (fresh install / empty store — daily stays 0 until more production today).
        new_baseline = state.last_total if state.last_total is not None else total_yield
        new_date = local_date
    else:
        new_baseline = state.baseline if state.baseline is not None else total_yield
        new_date = local_date

    if total_yield < new_baseline:
        # Lifetime counter went backwards — re-anchor rather than report negative day.
        new_baseline = total_yield
        daily = 0.0
    else:
        daily = total_yield - new_baseline

    new_state = DailyYieldBaseline(
        baseline=new_baseline,
        baseline_date=new_date,
        last_total=total_yield,
    )
    return round(daily, 3), new_state


def apply_derived_daily_yield(
    data: dict[str, Any],
    *,
    local_date: date,
    state: DailyYieldBaseline,
) -> tuple[dict[str, Any], DailyYieldBaseline, float | None]:
    """Overwrite ``daily_yield`` from ``total_yield`` when lifetime total is present.

    Returns ``(data, new_state, daily_or_None)``. Leaves ``data`` unchanged when
    ``total_yield`` is missing or not numeric.
    """
    total_point = data.get("total_yield")
    if not isinstance(total_point, dict):
        return data, state, None
    total = _as_float(total_point.get("value"))
    if total is None:
        return data, state, None

    daily, new_state = step_daily_yield(total, local_date, state)
    unit = total_point.get("unit") or "kWh"
    existing_raw = data.get("daily_yield")
    existing: dict[str, Any] = existing_raw if isinstance(existing_raw, dict) else {}
    data = {
        **data,
        "daily_yield": {
            **existing,
            "code": "daily_yield",
            "value": daily,
            "unit": unit,
            # Distinct from the raw broken register so provenance stays honest.
            "source": "modbus_derived",
        },
    }
    return data, new_state, daily
