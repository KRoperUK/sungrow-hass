"""Derive calendar-day energy from a lifetime counter.

Shared by every local-Modbus daily register that cannot be trusted to mean "today".
Two families of registers qualify, and both use the same subtraction and baseline:

* **Daily yield** (#223 / Modbus SG-RS): on several SG-RS + WiNet-S firmwares the
  documented "Daily power yields" register (wire 5002) never resets at midnight — it
  climbs in lockstep with lifetime energy and reports a multi-day cumulative value.
* **Daily grid import/export** (#471): the daily import/export registers are
  firmware-dependent — some SH firmware answers a flat 0 (#401) — and the points are
  omitted entirely when no external grid meter is fitted (#387). The lifetime
  ``total_imported_energy`` / ``total_exported_energy`` counters are reliable.

Both use the same subtraction:

    daily = total − total_at_start_of_local_day

The baseline is the last ``total`` observed on the previous local calendar day
(approximately end-of-yesterday / start-of-today). State is meant to be persisted
across restarts by the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class DerivedDailyBaseline:
    """Mutable baseline used to derive today's figure from a lifetime counter."""

    # Lifetime total at the start of ``baseline_date`` (local calendar day).
    baseline: float | None = None
    baseline_date: date | None = None
    # Most recent lifetime total sample (used as the next day's baseline on rollover).
    last_total: float | None = None

    def to_store(self) -> dict[str, Any]:
        """Serialize for HA Store."""
        return {
            "baseline": self.baseline,
            "baseline_date": self.baseline_date.isoformat() if self.baseline_date else None,
            "last_total": self.last_total,
        }

    @classmethod
    def from_store(cls, data: dict[str, Any] | None) -> DerivedDailyBaseline:
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
    except TypeError, ValueError:
        return None


def _usable_anchor(value: float | None, total: float) -> float:
    """Return a usable start-of-day baseline, re-anchoring at ``total`` when unusable.

    ``None`` means "no history yet". A non-positive value is equally unusable: a lifetime
    counter reading 0 (a firmware reboot, or a block that glitched at the day boundary)
    is never a plausible start-of-day anchor for a plant that is now reporting a
    non-zero lifetime total. Subtracting either would make ``daily`` report the plant's
    *entire* lifetime yield — the bug behind #400 — so both re-anchor to the current
    total and daily restarts from 0 instead.
    """
    if value is None or value <= 0:
        return total
    return value


def step_derived_daily(
    total: float,
    local_date: date,
    state: DerivedDailyBaseline,
    first_anchor: float | None = None,
) -> tuple[float, DerivedDailyBaseline]:
    """Advance baseline state for one lifetime-counter sample; return (daily, new_state).

    * On the first sample of a new local calendar day, the baseline becomes the
      previous sample's total (``last_total``), which is the best available estimate
      of energy at local midnight when polls run through the night.
    * On the first sample ever (no history), the baseline is ``first_anchor`` when the
      caller has a better estimate of the start-of-day total than the current one — the
      grid derivation seeds it from the device's own daily register, so the day it takes
      over doesn't report 0. It falls back to the current total, so daily starts at 0
      until the next midnight (midday install / empty store).
    * A missing or non-positive baseline (``None`` or 0) is re-anchored to the current
      total, so a lifetime counter that read 0 at the day boundary can't make daily
      report the whole lifetime counter (#400).
    * If total drops below the baseline (meter reset / firmware glitch), the baseline
      resets to the new total and daily is 0.
    """
    if state.baseline_date != local_date:
        # Prefer yesterday's last sample as start-of-today; on a genuinely fresh start the
        # caller's seed beats the current total, which would report a day of 0.
        anchor = state.last_total if state.last_total is not None else first_anchor
        new_baseline = _usable_anchor(anchor, total)
        new_date = local_date
    else:
        new_baseline = _usable_anchor(state.baseline, total)
        new_date = local_date

    if total < new_baseline:
        # Lifetime counter went backwards — re-anchor rather than report negative day.
        new_baseline = total
        daily = 0.0
    else:
        daily = total - new_baseline

    new_state = DerivedDailyBaseline(
        baseline=new_baseline,
        baseline_date=new_date,
        last_total=total,
    )
    return round(daily, 3), new_state


def apply_derived_daily_yield(
    data: dict[str, Any],
    *,
    local_date: date,
    state: DerivedDailyBaseline,
) -> tuple[dict[str, Any], DerivedDailyBaseline, float | None]:
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

    daily, new_state = step_derived_daily(total, local_date, state)
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


# Ceiling used to spot a lifetime counter that has gone to garbage rather than counted.
# Deliberately far above any domestic service (~145 A three-phase): its only job is to
# reject an impossible jump, not to model the site's supply. A disconnected smart meter
# is the known cause — the inverter keeps answering these registers with sentinel-adjacent
# values (mkaiser#692), and an upward jump has no other guard: the baseline logic below
# only re-anchors on a *decrease*.
MAX_GRID_POWER_W = 100_000


def implausible_counter_jump(previous: float | None, current: float, elapsed_seconds: float | None) -> bool:
    """Return whether a lifetime counter moved further than the wiring could carry.

    Compares the step against ``MAX_GRID_POWER_W`` over the time since the previous
    sample, so it fails open in every direction that matters: no previous sample (first
    poll), no elapsed time (restart), or a long gap between polls (HA was down, a poll
    backed off) all raise or remove the allowance rather than rejecting real catch-up.
    """
    if previous is None or elapsed_seconds is None or elapsed_seconds <= 0:
        return False
    allowed_kwh = MAX_GRID_POWER_W * elapsed_seconds / 3_600_000
    return (current - previous) > allowed_kwh


# Lifetime counter -> daily counter pairs derived locally (#471). Keyed by the local
# Modbus register codes, which are the point ids `resolve_classification` sees there.
# Both families the integration maps expose a lifetime grid counter that tracks the
# meter even when the daily register does not, so this is deliberately not family-gated
# the way `needs_derived_daily_yield` is for yield: the daily grid registers are
# firmware-dependent on SG and SH alike.
DERIVED_DAILY_COUNTER_PAIRS: tuple[tuple[str, str], ...] = (
    ("total_imported_energy", "daily_imported_energy"),
    ("total_exported_energy", "daily_exported_energy"),
)


@dataclass
class DerivedDailyEnergyState:
    """Per-lifetime-counter baselines for locally derived daily grid energy (#471).

    One :class:`DerivedDailyBaseline` per lifetime code, because each counter
    (import / export) crosses midnight independently.
    """

    baselines: dict[str, DerivedDailyBaseline] = field(default_factory=dict)

    def to_store(self) -> dict[str, Any]:
        """Serialize for HA Store (keyed by the lifetime code)."""
        return {code: baseline.to_store() for code, baseline in self.baselines.items()}

    @classmethod
    def from_store(cls, data: dict[str, Any] | None) -> DerivedDailyEnergyState:
        """Restore from HA Store (tolerant of missing/partial payloads)."""
        if not data:
            return cls()
        baselines = {
            str(code): DerivedDailyBaseline.from_store(raw) for code, raw in data.items() if isinstance(raw, dict)
        }
        return cls(baselines=baselines)


def apply_derived_daily_grid_energy(
    data: dict[str, Any],
    *,
    local_date: date,
    state: DerivedDailyEnergyState,
    untrusted: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], DerivedDailyEnergyState, dict[str, float]]:
    """Fill unreliable daily grid import/export from the lifetime counters (#471).

    ``daily_imported_energy`` / ``daily_exported_energy`` are firmware-dependent: some
    SH firmware answers a flat 0 instead of counting the day (#401), and the points are
    dropped entirely when no external meter is fitted (#387). The lifetime counters are
    reliable, so "today" is ``total − total at the start of the local day`` — the same
    derivation ``daily_yield`` uses for wire 5002.

    While a lifetime counter is present the derived value *replaces* the device's own
    daily register rather than only filling in a missing/zero one. That is deliberate:
    the sensor platform classifies a derived value as ``ENERGY``/``TOTAL_INCREASING`` so
    the Energy dashboard can use it, and it picks that class once, when the entity is
    built. Letting the source alternate between the raw register and our arithmetic
    through the day would make the class depend on when Home Assistant last restarted.
    So the day we take over is seeded from the device's own daily register
    (``total − daily``), and nothing is lost by the switch.

    A counter whose lifetime total is absent is skipped, so a meterless plant stays
    silent rather than publishing a fabricated ``0`` (the #387 contract).

    ``untrusted`` names lifetime codes whose latest sample the caller has already rejected
    (see :func:`implausible_counter_jump`). Those are left alone entirely — no derived
    value, no baseline movement — so a counter that returns to sane values later resumes
    the day where it left off, and the entity reads what the device itself reports in the
    meantime rather than a spike.

    Returns ``(data, new_state, derived)``; ``derived`` maps each daily code to the value
    published for it (empty when nothing was derived).
    """
    baselines = dict(state.baselines)
    derived: dict[str, float] = {}
    for total_code, daily_code in DERIVED_DAILY_COUNTER_PAIRS:
        if total_code in untrusted:
            continue
        total_point = data.get(total_code)
        if not isinstance(total_point, dict):
            continue
        total = _as_float(total_point.get("value"))
        if total is None:
            continue

        baseline = baselines.get(total_code)
        raw_point = data.get(daily_code)
        raw = _as_float(raw_point.get("value")) if isinstance(raw_point, dict) else None

        # First sample ever: prefer the device's own daily figure as the start-of-day
        # estimate, so the takeover doesn't report 0 for the rest of the day. Only a
        # plausible reading is trusted (a flat 0 or a value above the lifetime counter
        # tells us nothing), and it is never used once we have history to anchor on. A
        # stored entry with no date carries no history either — treat it the same as none.
        no_history = baseline is None or baseline.baseline_date is None
        first_anchor = total - raw if no_history and raw is not None and 0 < raw <= total else None

        daily, baselines[total_code] = step_derived_daily(
            total, local_date, baseline or DerivedDailyBaseline(), first_anchor
        )

        unit = total_point.get("unit") or "kWh"
        point: dict[str, Any] = {
            "code": daily_code,
            "value": daily,
            "unit": unit,
            # Distinct from the raw register so provenance stays honest.
            "source": "modbus_derived",
        }
        # Keep the device's own reading alongside ours. It is the other half of the
        # comparison a support thread always ends up asking for ("what does the register
        # say?"), and a flat 0 next to a real derived figure is the fastest way to see
        # that the register is the broken side (#401/#471).
        if raw is not None:
            point["raw_register_value"] = raw
        data = {**data, daily_code: point}
        derived[daily_code] = daily
    return data, DerivedDailyEnergyState(baselines=baselines), derived
