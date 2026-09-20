"""Tests for the derived battery pack-health spreads (#430)."""

from custom_components.sungrow.pack_health import add_pack_health_points


def _point(value, unit, source="cloud_user"):
    return {"code": "x", "value": value, "unit": unit, "source": source}


def _battery_payload(**overrides):
    points = {
        "battery_max_cell_voltage": _point(3345, "mV"),
        "battery_min_cell_voltage": _point(3320, "mV"),
        "battery_max_module_temperature": _point(28.5, "°C"),
        "battery_min_module_temperature": _point(24.0, "°C"),
    }
    points.update(overrides)
    return points


def test_spreads_are_computed_from_the_extremes():
    """Both spreads are the max−min of the pair, in the sources' own unit."""
    out = add_pack_health_points(_battery_payload())

    assert out["cell_imbalance"]["value"] == 25
    assert out["cell_imbalance"]["unit"] == "mV"
    assert out["module_temperature_spread"]["value"] == 4.5
    assert out["module_temperature_spread"]["unit"] == "°C"


def test_derived_points_mark_themselves_as_derived():
    """``source`` keeps the transport and says the value is ours, not the device's."""
    out = add_pack_health_points(_battery_payload())

    assert out["cell_imbalance"]["source"] == "cloud_user_derived"
    assert out["module_temperature_spread"]["source"] == "cloud_user_derived"


def test_source_points_are_left_untouched():
    """The derivation only adds; the raw extremes stay exactly as fetched."""
    points = _battery_payload()

    out = add_pack_health_points(points)

    assert out["battery_max_cell_voltage"] == points["battery_max_cell_voltage"]
    assert out["battery_min_cell_voltage"] == points["battery_min_cell_voltage"]
    # Input is not mutated either.
    assert "cell_imbalance" not in points


def test_zero_spread_is_published():
    """A perfectly balanced pack reports 0, which is meaningful — not a gap."""
    points = _battery_payload(battery_max_cell_voltage=_point(3300, "mV"), battery_min_cell_voltage=_point(3300, "mV"))

    assert add_pack_health_points(points)["cell_imbalance"]["value"] == 0.0


def test_inverted_pair_is_skipped():
    """max < min cannot happen physically, so the arithmetic on top of it is suppressed."""
    points = _battery_payload(battery_max_cell_voltage=_point(3200, "mV"), battery_min_cell_voltage=_point(3345, "mV"))

    assert "cell_imbalance" not in add_pack_health_points(points)


def test_half_a_pair_is_skipped():
    """A spread needs both extremes; a device reporting only one gets nothing."""
    points = _battery_payload()
    del points["battery_min_cell_voltage"]

    assert "cell_imbalance" not in add_pack_health_points(points)


def test_unparseable_value_is_skipped():
    """The API's placeholder values must not become arithmetic."""
    points = _battery_payload(battery_max_cell_voltage=_point("--", "mV"))

    assert "cell_imbalance" not in add_pack_health_points(points)


def test_non_battery_device_is_returned_unchanged():
    """Devices with no cell/module detail are untouched, so this is safe to run on all."""
    points = {"battery_level": _point(72.6, "%")}

    assert add_pack_health_points(points) == points
