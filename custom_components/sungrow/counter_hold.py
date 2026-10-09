"""Hold server-derived lifetime counters steady through small downward dips (#487).

iSolarCloud computes some lifetime energy totals server-side rather than reading them off
a meter — the load-consumption totals are the clearest case, being a balance of PV, grid
and battery flows. When the server recomputes that balance the total can step *down* by a
fraction of a kWh (e.g. 8635.66 → 8635.56 kWh), which Home Assistant's recorder reports as
"state class total_increasing, but its state is not strictly increasing", and which it
then reads as a meter reset. The *metered* lifetime totals (grid import/export, battery
charge/discharge, PV yield) are less prone to it but not immune — a stale re-published
sample can wobble downward too — so the hold now covers them as well (#490), with a tighter
threshold since a real downward step on a metered counter is more likely to be a reset.

The fix that keeps these sensors useful is to stay ``ENERGY``/``TOTAL_INCREASING`` (so they
remain Energy-dashboard sources) and publish the last good value while the server's figure
sits slightly below it. Once the server's figure climbs back past the held value it is
published again, so no energy is lost. A *large* drop is not noise: it is passed through so
Home Assistant sees a genuine counter reset as one.

The rules are table-driven (:data:`HELD_LIFETIME_COUNTERS`), one :class:`CounterDipRule` per
counter so server-derived and metered totals can carry different dip thresholds. The id/code
list mirrors the integration's single curated set of lifetime ``TOTAL_INCREASING``
identifiers (``measure_points._CUMULATIVE_ENERGY_POINT_IDS``). This guards *downward dips*
only; the complementary *upward-spike* guard for a disconnected meter lives in
``coordinator._untrusted_lifetime_counters`` (#475/#471) and is unaffected. The last good
value lives in memory only: after a restart the first value is accepted as-is, since there
is nothing trustworthy to compare it against.
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
# far from both. Used for the *server-derived* totals (load consumption), where the whole
# value is a recomputed balance and a small step down is expected.
_SERVER_DERIVED_TOTAL = CounterDipRule(max_dip_fraction=0.10)

# Metered / device-reported lifetime totals (grid import/export, battery charge/discharge,
# PV yield). These are read off a meter or accumulated on the device rather than recomputed,
# so a *downward* step is far less expected than on a server-derived balance — but it still
# happens (a rounding wobble when the cloud re-publishes a slightly stale sample, #490) and
# trips the recorder's ``total_increasing`` non-monotonic warning just the same. A tighter
# 1% band holds that wobble while leaving a genuine meter reset (a fall to near zero, orders
# of magnitude past 1%) to pass straight through as a real reset. Deliberately conservative:
# the cost of too-tight is a rare spurious warning; too-loose would silence a real reset.
_METERED_TOTAL = CounterDipRule(max_dip_fraction=0.01)

# Measure-point id -> rule. Keyed by the numeric point id (the ``id`` field of a point, or
# its key on the cloud_user/Modbus transport, where ``point_id`` falls back to the bare
# code), so one entry covers every transport and payload that reports the point. The id/code
# list mirrors ``measure_points._CUMULATIVE_ENERGY_POINT_IDS`` — the single curated set of
# lifetime TOTAL_INCREASING identifiers across OAuth / cloud_user / Modbus — so a counter the
# integration already treats as monotonic also gets the dip-hold (#490). Kept as an explicit
# table rather than imported so each counter's *threshold* can differ (server-derived vs
# metered) and the mapping stays auditable.
#
# NB this guards *downward dips* only. The separate upward-spike guard for a disconnected
# meter (``coordinator._untrusted_lifetime_counters``, driven by ``DERIVED_DAILY_COUNTERS``)
# is unchanged and complementary — the two cover opposite failure modes (#475/#471 vs #487).
HELD_LIFETIME_COUNTERS: dict[str, CounterDipRule] = {
    # Server-derived load-consumption totals, plant-level and per-device (ESS) — #487.
    "83124": _SERVER_DERIVED_TOTAL,  # Total Load Consumption (plant)
    "13130": _SERVER_DERIVED_TOTAL,  # Total Load Consumption (ESS device)
    "13137": _SERVER_DERIVED_TOTAL,  # Total Load Energy Consumption from PV (ESS device)
    # Battery lifetime charge/discharge energy (cloud ids + Modbus codes) — #490.
    "58606": _METERED_TOTAL,  # Total Battery Charging Energy (common-battery)
    "58607": _METERED_TOTAL,  # Total Battery Discharging Energy (common-battery)
    "13034": _METERED_TOTAL,  # Total Battery Charging Energy (ESS inverter)
    "13035": _METERED_TOTAL,  # Total Battery Discharging Energy (ESS inverter)
    "13176": _METERED_TOTAL,  # Total Battery Charging Energy from PV
    "24622": _METERED_TOTAL,  # ESS Total Charge (EMS device)
    "24623": _METERED_TOTAL,  # ESS Total Discharge (EMS device)
    "total_battery_charge": _METERED_TOTAL,  # Local Modbus
    "total_battery_discharge": _METERED_TOTAL,  # Local Modbus
    "total_battery_charge_from_pv": _METERED_TOTAL,  # Local Modbus
    # Grid import/export lifetime energy (cloud ids + Modbus codes) — #490.
    "8030": _METERED_TOTAL,  # Meter Forward Active Energy (lifetime import)
    "8031": _METERED_TOTAL,  # Meter Reverse Active Energy (lifetime export)
    "13125": _METERED_TOTAL,  # Total Feed-in Energy (ESS inverter)
    "13148": _METERED_TOTAL,  # Total Purchased Energy (ESS inverter)
    "13175": _METERED_TOTAL,  # Total Feed-in Energy (PV) — OAuth
    "83123": _METERED_TOTAL,  # Total Feed-in Energy (PV) — user-cloud getPsDetail
    "83075": _METERED_TOTAL,  # Feed-in Energy Total — open API
    "total_imported_energy": _METERED_TOTAL,  # Local Modbus
    "total_exported_energy": _METERED_TOTAL,  # Local Modbus
    "total_exported_energy_from_pv": _METERED_TOTAL,  # Local Modbus
    # PV lifetime yield (cloud ids + Modbus codes) — #490.
    "13134": _METERED_TOTAL,  # Total PV Yield (cloud)
    "total_yield": _METERED_TOTAL,  # Local Modbus
    "total_pv_gen_battery_discharge": _METERED_TOTAL,  # Local Modbus
    "total_direct_energy_consumption": _METERED_TOTAL,  # Local Modbus
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
