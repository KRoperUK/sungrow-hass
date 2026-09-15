"""Tests for the real battery charge/discharge power-ceiling resolution (#450).

The app battery endpoints (``getBatteryCapacityByPsIdV2`` / ``getPsBatteryInfo``) are
consumed best-effort to size the dispatch power slider from real hardware instead of the
static default. The observed capacity payload carries kWh capacity + battery type (no
power field), so the resolver must return ``None`` for it and only override when a real,
unit-qualified power figure appears — which is live-untested against a device.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pysolarcloud import PySolarCloudException

from custom_components.sungrow.coordinator import (
    _coerce_power_watts,
    resolve_battery_power_limit_w,
)


def test_capacity_only_payload_yields_no_power_limit():
    """The observed capacity shape (kWh + battery type) has no power field -> None."""
    capacity = {"batteryType": 1, "isCanChangeBatteryType": 0, "list": [{"capacity": "9.6", "unit": "kWh"}]}
    assert resolve_battery_power_limit_w(capacity, {}) is None


def test_none_and_nondict_payloads_are_ignored():
    """Missing/malformed payloads never raise and resolve to None."""
    assert resolve_battery_power_limit_w(None, None) is None
    assert resolve_battery_power_limit_w("nope", 5) is None  # type: ignore[arg-type]


def test_kw_power_field_converted_to_watts():
    """A charge/discharge power field in kW is converted to W."""
    payload = {"max_charge_power": {"value": "10.6", "unit": "kW"}}
    assert resolve_battery_power_limit_w(payload) == 10600


def test_watt_power_field_taken_as_is():
    """A power field already in W is trusted as-is."""
    assert resolve_battery_power_limit_w({"max_discharge_power": {"value": "8000", "unit": "W"}}) == 8000


def test_largest_of_charge_and_discharge_wins():
    """Charge and discharge share one slider ceiling: the larger wins."""
    payload = {
        "max_charge_power": {"value": "6600", "unit": "W"},
        "max_discharge_power": {"value": "5000", "unit": "W"},
    }
    assert resolve_battery_power_limit_w(payload) == 6600


def test_bare_kilowatt_number_rejected_to_avoid_magnitude_error():
    """A bare number that looks like kW (no unit) is rejected, not mis-scaled 1000x."""
    # 10 with no unit could be 10 kW or 10 W; below the plausible-watt floor -> rejected.
    assert _coerce_power_watts(10) is None
    # A plausible-watt bare number is accepted.
    assert _coerce_power_watts(8000) == 8000


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -5, {"value": None}])
def test_coerce_power_watts_rejects_non_power(bad):
    """Non-numeric, zero and negative values coerce to None."""
    assert _coerce_power_watts(bad) is None


async def test_probe_stores_limit_when_power_field_present():
    """The coordinator probe stores a resolved limit from the battery endpoints (#450)."""
    from custom_components.sungrow.coordinator import SungrowPlantCoordinator

    coordinator = MagicMock(spec=SungrowPlantCoordinator)
    coordinator.has_battery = True
    coordinator.plant_id = "5"
    coordinator.plant_name = "Home"
    coordinator._poll_timeout = 10
    user_auth = MagicMock()
    user_auth.async_get_battery_capacity = AsyncMock(return_value={"max_charge_power": {"value": "9", "unit": "kW"}})
    user_auth.async_get_battery_info = AsyncMock(return_value={})
    coordinator._user_auth = user_auth
    coordinator.battery_power_limit_w = None
    coordinator.battery_capacity = {}

    await SungrowPlantCoordinator.async_probe_battery_power_limit(coordinator)

    assert coordinator.battery_power_limit_w == 9000


async def test_probe_is_noop_without_battery():
    """No battery -> the probe returns immediately and makes no calls (#148/#450)."""
    from custom_components.sungrow.coordinator import SungrowPlantCoordinator

    coordinator = MagicMock(spec=SungrowPlantCoordinator)
    coordinator.has_battery = False
    user_auth = MagicMock()
    user_auth.async_get_battery_capacity = AsyncMock()
    coordinator._user_auth = user_auth
    coordinator.battery_power_limit_w = None

    await SungrowPlantCoordinator.async_probe_battery_power_limit(coordinator)

    user_auth.async_get_battery_capacity.assert_not_awaited()
    assert coordinator.battery_power_limit_w is None


async def test_probe_degrades_gracefully_on_api_error():
    """An API error leaves the limit unresolved (existing behaviour preserved) (#450)."""
    from custom_components.sungrow.coordinator import SungrowPlantCoordinator

    coordinator = MagicMock(spec=SungrowPlantCoordinator)
    coordinator.has_battery = True
    coordinator.plant_id = "5"
    coordinator.plant_name = "Home"
    coordinator._poll_timeout = 10
    user_auth = MagicMock()
    user_auth.async_get_battery_capacity = AsyncMock(side_effect=PySolarCloudException("boom"))
    user_auth.async_get_battery_info = AsyncMock(side_effect=PySolarCloudException("boom"))
    coordinator._user_auth = user_auth
    coordinator.battery_power_limit_w = None
    coordinator.battery_capacity = {}

    await SungrowPlantCoordinator.async_probe_battery_power_limit(coordinator)

    assert coordinator.battery_power_limit_w is None
