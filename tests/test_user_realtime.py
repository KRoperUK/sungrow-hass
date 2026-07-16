"""Tests for the user-account getPsDetail -> measure-point mapper (#269)."""

from custom_components.sungrow.user_realtime import map_plant_detail_to_points


def test_maps_named_current_power():
    """curr_power becomes a current_power point with its unit."""
    points = map_plant_detail_to_points({"curr_power": {"value": "3200", "unit": "W"}})
    assert points["current_power"] == {"id": "current_power", "code": "current_power", "value": "3200", "unit": "W"}


def test_prefers_virgin_raw_value_over_display():
    """The raw _map_virgin value (real base unit) wins over the display _map value."""
    detail = {
        "p83106_map": {"value": "12.5", "unit": ""},
        "p83106_map_virgin": {"value": "12500", "unit": "Wh"},
    }
    points = map_plant_detail_to_points(detail)
    assert points["p83106"] == {"id": "83106", "code": "p83106", "value": "12500", "unit": "Wh"}


def test_falls_back_to_map_without_virgin():
    """A point with only a display _map field is still mapped."""
    points = map_plant_detail_to_points({"p83022_map": {"value": "5230", "unit": "W"}})
    assert points["p83022"] == {"id": "83022", "code": "p83022", "value": "5230", "unit": "W"}


def test_scalar_counts_mapped_unitless():
    """alarm_count / fault_count become unitless diagnostic points."""
    points = map_plant_detail_to_points({"alarm_count": 0, "fault_count": 2})
    assert points["alarm_count"]["value"] == 0
    assert points["fault_count"]["value"] == 2
    assert points["fault_count"]["unit"] == ""


def test_skips_empty_and_nondict_values():
    """Empty values and non-point fields are ignored."""
    detail = {
        "p1_map_virgin": {"value": "", "unit": "Wh"},  # empty -> skipped
        "p2_map": {"value": None, "unit": "W"},  # None -> skipped
        "build_date": "2020-01-01",  # not a point field
        "images": [],  # non-dict
        "curr_power": {"value": "100", "unit": "W"},
    }
    points = map_plant_detail_to_points(detail)
    assert "p1" not in points
    assert "p2" not in points
    assert set(points) == {"current_power"}


def test_map_virgin_regex_not_confused_by_suffix():
    """p<ID>_percent and other suffixes are not treated as points."""
    points = map_plant_detail_to_points({"p83102_percent": "42", "p83102_map_virgin": {"value": "9", "unit": "kWh"}})
    assert set(points) == {"p83102"}
    assert points["p83102"]["unit"] == "kWh"
