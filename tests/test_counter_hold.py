"""Tests for holding server-derived lifetime counters through small dips (#487)."""

import logging

import pytest

from custom_components.sungrow.counter_hold import HELD_LIFETIME_COUNTERS, LifetimeCounterHold


def _load(value, *, key="total_load_consumption", point_id="83124"):
    return {key: {"id": point_id, "code": key, "value": value, "unit": "kWh", "source": "cloud"}}


def test_first_value_is_accepted_as_is():
    """With nothing to compare against (fresh start / restart) the first reading stands."""
    hold = LifetimeCounterHold()
    data = _load(8635.66)
    assert hold.apply("plant", data) is data


def test_small_dip_is_held_at_the_last_good_value(caplog):
    """The reported 8635.66 → 8635.56 dip publishes 8635.66 and is debug-logged."""
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(8635.66))

    with caplog.at_level(logging.DEBUG, logger="custom_components.sungrow.counter_hold"):
        out = hold.apply("plant", _load(8635.56), label="Plant")

    assert out["total_load_consumption"]["value"] == 8635.66
    # The rest of the point is preserved; only the value is held.
    assert out["total_load_consumption"]["unit"] == "kWh"
    assert out["total_load_consumption"]["source"] == "cloud"
    assert "Holding total_load_consumption on Plant at 8635.66" in caplog.text


def test_held_input_is_not_mutated():
    """The coordinator's raw payload stays untouched; a copy carries the held value."""
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(8635.66))
    raw = _load(8635.56)
    hold.apply("plant", raw)
    assert raw["total_load_consumption"]["value"] == 8635.56


def test_hold_survives_successive_dips_and_recovers():
    """A dip stays held across polls until the server passes the last good value again."""
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(8635.66))

    assert hold.apply("plant", _load(8635.56))["total_load_consumption"]["value"] == 8635.66
    assert hold.apply("plant", _load(8635.60))["total_load_consumption"]["value"] == 8635.66
    # Recovered: the real figure is published again and becomes the new reference.
    assert hold.apply("plant", _load(8635.80))["total_load_consumption"]["value"] == 8635.80
    assert hold.apply("plant", _load(8635.70))["total_load_consumption"]["value"] == 8635.80


def test_equal_reading_is_not_a_dip():
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(8635.66))
    assert hold.apply("plant", _load(8635.66))["total_load_consumption"]["value"] == 8635.66


def test_large_drop_passes_through_as_a_genuine_reset(caplog):
    """A drop beyond the threshold (a real counter reset) is published and re-anchors."""
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(8635.66))

    with caplog.at_level(logging.DEBUG, logger="custom_components.sungrow.counter_hold"):
        out = hold.apply("plant", _load(3.2))

    assert out["total_load_consumption"]["value"] == 3.2
    assert "treated as a genuine counter reset" in caplog.text
    # The reset value is the new reference, so the counter counts on from there.
    assert hold.apply("plant", _load(3.1))["total_load_consumption"]["value"] == 3.2


def test_threshold_boundary():
    """Exactly 10% is still noise; just beyond it is a reset."""
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(1000.0))
    assert hold.apply("plant", _load(900.0))["total_load_consumption"]["value"] == 1000.0
    assert hold.apply("plant", _load(899.0))["total_load_consumption"]["value"] == 899.0


@pytest.mark.parametrize(
    ("key", "point_id"),
    [
        # OAuth plant payload: readable code as key, numeric id inside.
        ("total_load_consumption", "83124"),
        # cloud_user payload: the bare id is the key and the code.
        ("83124", "83124"),
        # ESS per-device totals.
        ("13130", "13130"),
        ("13137", "13137"),
    ],
)
def test_matches_every_load_total_by_point_id(key, point_id):
    hold = LifetimeCounterHold()
    hold.apply("plant", _load(500.0, key=key, point_id=point_id))
    assert hold.apply("plant", _load(499.9, key=key, point_id=point_id))[key]["value"] == 500.0


def test_scopes_are_independent():
    """Two devices reporting the same point id never hold against each other."""
    hold = LifetimeCounterHold()
    hold.apply("device:a", _load(500.0, key="13130", point_id="13130"))
    out = hold.apply("device:b", _load(120.0, key="13130", point_id="13130"))
    assert out["13130"]["value"] == 120.0


def test_unlisted_counters_and_odd_values_are_left_alone():
    """Only table entries are held; non-numeric readings neither hold nor reset the reference."""
    hold = LifetimeCounterHold()
    other = {"total_yield": {"id": "83022", "code": "total_yield", "value": 10.0}}
    hold.apply("plant", other)
    dipped = {"total_yield": {"id": "83022", "code": "total_yield", "value": 9.9}}
    assert hold.apply("plant", dipped) is dipped

    hold.apply("plant", _load(500.0))
    assert hold.apply("plant", _load(""))["total_load_consumption"]["value"] == ""
    assert hold.apply("plant", _load(499.9))["total_load_consumption"]["value"] == 500.0


def test_table_covers_the_server_derived_load_totals():
    assert set(HELD_LIFETIME_COUNTERS) == {"83124", "13130", "13137"}
