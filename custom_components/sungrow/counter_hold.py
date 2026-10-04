"""Hold server-derived lifetime counters steady through small downward dips (#487).

iSolarCloud computes some lifetime energy totals server-side rather than reading them off
a meter — the load-consumption totals are the clearest case, being a balance of PV, grid
and battery flows. When the server recomputes that balance the total can step *down* by a
fraction of a kWh (e.g. 8635.66 → 8635.56 kWh), which Home Assistant's recorder reports as
"state class total_increasing, but its state is not strictly increasing", and which it
then reads as a meter reset.

The fix that keeps these sensors useful is to stay ``ENERGY``/``TOTAL_INCREASING`` (so they
remain Energy-dashboard sources) and publish the last good value while the server's figure
sits slightly below it. Once the server's figure climbs back past the held value it is
published again, so no energy is lost. A *large* drop is not noise: it is passed through so
Home Assistant sees a genuine counter reset as one.

The rules are table-driven (:data:`HELD_LIFETIME_COUNTERS`) so other counters can opt in
later. The last good value lives in memory only: after a restart the first value is
accepted as-is, since there is nothing trustworthy to compare it against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CounterDipRule:
    """How far below the last good value a reading may dip and still be held.

    ``max_dip_fraction`` is a fraction of the last good value. A dip at or below it is
    treated as server-side recompute noise and held; anything larger is passed through as
    a genuine counter reset.
    """

    max_dip_fraction: float


# A dip of up to 10% of the lifetime total is noise; a larger one is a reset. The observed
# noise is a few hundredths of a percent, and a real reset falls to (near) zero, so 10% sits
# far from both.
_SERVER_DERIVED_TOTAL = CounterDipRule(max_dip_fraction=0.10)

# Measure-point id -> rule. Keyed by the numeric point id (the ``id`` field of a point, or
# its key on the cloud_user transport, which uses the bare id as its code), so one entry
# covers every transport and payload that reports the point.
#
# The server-derived load-consumption lifetime totals, plant-level and per-device (ESS),
# from the iSolarCloud measure-point catalog (``measure_points_data.RAW_POINTS``):
HELD_LIFETIME_COUNTERS: dict[str, CounterDipRule] = {
    "83124": _SERVER_DERIVED_TOTAL,  # Total Load Consumption (plant)
    "13130": _SERVER_DERIVED_TOTAL,  # Total Load Consumption (ESS device)
    "13137": _SERVER_DERIVED_TOTAL,  # Total Load Energy Consumption from PV (ESS device)
}


def _as_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


@dataclass
class LifetimeCounterHold:
    """Remember the last good value per counter and hold small dips at it.

    ``scope`` separates counters that share a point id but belong to different sources —
    the plant payload and each device's payload — so one device's total is never compared
    against another's.
    """

    rules: dict[str, CounterDipRule] = field(default_factory=lambda: dict(HELD_LIFETIME_COUNTERS))
    _last_good: dict[tuple[str, str], float] = field(default_factory=dict)

    def apply(self, scope: str, data: dict[str, Any], *, label: str = "") -> dict[str, Any]:
        """Return ``data`` with any held counter's dip replaced by its last good value.

        Untouched when no point matches a rule, so the common case allocates nothing.
        """
        out: dict[str, Any] | None = None
        for key, point in data.items():
            if not isinstance(point, dict):
                continue
            point_id = str(point.get("id") or key)
            rule = self.rules.get(point_id)
            if rule is None:
                continue
            current = _as_float(point.get("value"))
            if current is None:
                continue
            slot = (scope, point_id)
            last = self._last_good.get(slot)
            if last is not None and current < last:
                if last - current <= last * rule.max_dip_fraction:
                    _LOGGER.debug(
                        "Holding %s on %s at %s: the server reported %s, a dip within %.0f%% "
                        "of the lifetime total, treated as recompute noise (#487)",
                        key,
                        label or scope,
                        last,
                        current,
                        rule.max_dip_fraction * 100,
                    )
                    if out is None:
                        out = dict(data)
                    out[key] = {**point, "value": last}
                    continue
                _LOGGER.debug(
                    "Passing through %s on %s: it fell from %s to %s, more than %.0f%%, so it is "
                    "treated as a genuine counter reset (#487)",
                    key,
                    label or scope,
                    last,
                    current,
                    rule.max_dip_fraction * 100,
                )
            self._last_good[slot] = current
        return data if out is None else out
