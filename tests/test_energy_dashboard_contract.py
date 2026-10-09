"""Energy-Dashboard eligibility contract for dashboard-facing energy sensors (#489).

Home Assistant's Energy dashboard only accepts a sensor whose ``device_class`` is
``ENERGY`` and whose ``state_class`` is ``TOTAL`` or ``TOTAL_INCREASING``. The docs tell
users to add specific Sungrow sensors there (grid import/export, PV yield, battery
charge/discharge, load consumption — lifetime and derived-daily variants). Nothing used to
assert that those stay eligible, so #463 could silently reclassify
``daily_battery_charge`` / ``daily_battery_discharge`` to ``(None, MEASUREMENT)`` and drop
them from the dashboard without a failing test (the #486 regression).

This pins the contract by driving the real :func:`resolve_classification` — the same call
``sensor.py`` makes — over the integration's own source-of-truth sets:

* lifetime totals: :data:`measure_points._CUMULATIVE_ENERGY_POINT_IDS`
* derived-daily:   :data:`measure_points._DERIVED_ENERGY_CODES`

Changing either set (adding or removing a dashboard sensor) is therefore a deliberate,
reviewed diff that shows up here. The derived-daily codes are asserted with ``derived=True``
because that is how the coordinator emits them (``source: modbus_derived``); the *raw*
daily registers of the same name are intentionally **not** eligible (firmware-dependent,
#431/#400/#401) and are not part of this contract.
"""

from __future__ import annotations

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.sungrow.measure_points import (
    _CUMULATIVE_ENERGY_POINT_IDS,
    _DERIVED_ENERGY_CODES,
    resolve_classification,
)

# HA Energy-dashboard eligibility: ENERGY device class + an accumulating state class.
_DASHBOARD_STATE_CLASSES = {SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING}
_ENERGY_UNITS = {"Wh", "kWh", "MWh", "GWh"}


def _assert_dashboard_eligible(device_class, state_class, unit, label):
    assert device_class == SensorDeviceClass.ENERGY, f"{label}: device_class {device_class!r} != ENERGY"
    assert state_class in _DASHBOARD_STATE_CLASSES, f"{label}: state_class {state_class!r} not accumulating"
    assert unit in _ENERGY_UNITS, f"{label}: unit {unit!r} is not an energy unit"


# --- Explicit expected membership -------------------------------------------------------
# A second, hand-maintained copy of the dashboard-facing identifiers. The sync tests below
# assert these equal the live source sets, so a change to the production sets that isn't
# mirrored here (or vice versa) fails loudly — i.e. adding/removing a dashboard sensor is a
# deliberate two-place diff, per the issue's first acceptance criterion.
_EXPECTED_LIFETIME_IDS = {
    # Battery lifetime charge/discharge.
    "58606",
    "58607",
    "13034",
    "13035",
    "13176",
    "24622",
    "24623",
    "total_battery_charge",
    "total_battery_discharge",
    "total_battery_charge_from_pv",
    "total_pv_gen_battery_discharge",
    # Grid import/export lifetime.
    "8030",
    "8031",
    "13125",
    "13148",
    "13175",
    "83123",
    "83075",
    "total_imported_energy",
    "total_exported_energy",
    "total_exported_energy_from_pv",
    # PV / plant lifetime yield + direct consumption.
    "13134",
    "total_yield",
    "total_direct_energy_consumption",
}
_EXPECTED_DERIVED_CODES = {
    "daily_imported_energy",
    "daily_exported_energy",
    "daily_battery_charge",
    "daily_battery_discharge",
}


def test_lifetime_set_matches_expected():
    assert set(_CUMULATIVE_ENERGY_POINT_IDS) == _EXPECTED_LIFETIME_IDS


def test_derived_set_matches_expected():
    assert set(_DERIVED_ENERGY_CODES) == _EXPECTED_DERIVED_CODES


# --- The eligibility contract itself ----------------------------------------------------
# Lifetime totals carry Wh on the cloud transports and kWh on the local Modbus codes; both
# are energy units, and the classification is pinned by point id regardless of unit, so a
# representative energy unit per group is enough to exercise the real path.


@pytest.mark.parametrize("point_id", sorted(_CUMULATIVE_ENERGY_POINT_IDS))
def test_lifetime_totals_are_dashboard_eligible(point_id):
    """Every lifetime total resolves to ENERGY / TOTAL_INCREASING (non-derived path)."""
    unit = "kWh" if not point_id.isdigit() else "Wh"  # Modbus codes report kWh; cloud ids Wh.
    device_class, state_class = resolve_classification(unit, point_id, point_id, derived=False)
    _assert_dashboard_eligible(device_class, state_class, unit, point_id)


@pytest.mark.parametrize("code", sorted(_DERIVED_ENERGY_CODES))
def test_derived_daily_counters_are_dashboard_eligible(code):
    """Every derived-daily counter is eligible on the derived path (source: modbus_derived)."""
    # The coordinator emits derived points with the lifetime counter's unit or "kWh".
    device_class, state_class = resolve_classification("kWh", code, code, derived=True)
    _assert_dashboard_eligible(device_class, state_class, "kWh", code)


def test_raw_daily_battery_registers_are_not_dashboard_eligible():
    """Guard the other side of #431/#486: the RAW daily registers must stay MEASUREMENT.

    Only the integration-*derived* daily value is trustworthy; the raw firmware register of
    the same name is deliberately not an Energy-dashboard source, so classifying it as
    ENERGY would reintroduce the corruption #431 fixed.
    """
    for code in ("daily_battery_charge", "daily_battery_discharge"):
        device_class, state_class = resolve_classification("kWh", code, code, derived=False)
        assert device_class is None, f"{code}: raw daily register must not be ENERGY"
        assert state_class == SensorStateClass.MEASUREMENT


def test_would_have_caught_463_regression():
    """#463 set daily_battery_charge/discharge to (None, MEASUREMENT) even when derived,
    dropping them from the dashboard (#486). The derived path must yield ENERGY so that a
    future reversion of that fix fails here.
    """
    for code in ("daily_battery_charge", "daily_battery_discharge"):
        device_class, state_class = resolve_classification("kWh", code, code, derived=True)
        assert device_class == SensorDeviceClass.ENERGY
        assert state_class == SensorStateClass.TOTAL_INCREASING
