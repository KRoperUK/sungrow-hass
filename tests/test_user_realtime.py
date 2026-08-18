"""Tests for the user-account getPsDetail -> measure-point mapper (#269)."""

from custom_components.sungrow.measure_points import resolve_name
from custom_components.sungrow.user_realtime import map_plant_detail_to_points


def test_mapped_codes_resolve_to_catalog_names():
    """Bare-numeric codes let resolve_name find the catalog name (OAuth parity, #269)."""
    detail = {
        "p83124_map_virgin": {"value": "2923100", "unit": "Wh"},  # Total Load Consumption
        "p83202_map_virgin": {"value": "3600", "unit": "Wp"},  # Nominal Power
    }
    points = map_plant_detail_to_points(detail)
    total_load = points["83124"]
    assert resolve_name(total_load["id"], total_load["code"], None) == "Total Load Consumption"
    nameplate = points["83202"]
    assert resolve_name(nameplate["id"], nameplate["code"], None) == "Nominal Power"


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
    assert points["83106"] == {"id": "83106", "code": "83106", "value": "12500", "unit": "Wh"}


def test_falls_back_to_map_without_virgin():
    """A point with only a display _map field is still mapped."""
    points = map_plant_detail_to_points({"p83022_map": {"value": "5230", "unit": "W"}})
    assert points["83022"] == {"id": "83022", "code": "83022", "value": "5230", "unit": "W"}


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
    assert "1" not in points
    assert "2" not in points
    assert set(points) == {"current_power"}


def test_map_virgin_regex_not_confused_by_suffix():
    """p<ID>_percent and other suffixes are not treated as points."""
    points = map_plant_detail_to_points({"p83102_percent": "42", "p83102_map_virgin": {"value": "9", "unit": "kWh"}})
    assert set(points) == {"83102"}
    assert points["83102"]["unit"] == "kWh"


def test_maps_today_energy():
    """today_energy becomes a today_energy point (#292)."""
    points = map_plant_detail_to_points({"today_energy": {"value": "12.5", "unit": "kWh"}})
    assert points["today_energy"] == {"id": "today_energy", "code": "today_energy", "value": "12.5", "unit": "kWh"}


def test_maps_total_energy():
    """total_energy becomes a total_energy point (#292)."""
    points = map_plant_detail_to_points({"total_energy": {"value": "6800.0", "unit": "kWh"}})
    assert points["total_energy"]["value"] == "6800.0"
    assert points["total_energy"]["unit"] == "kWh"


def test_maps_co2_reduce_total():
    """co2_reduce_total becomes a co2_reduce_total point (#292)."""
    points = map_plant_detail_to_points({"co2_reduce_total": {"value": "4200.5", "unit": "kg"}})
    assert points["co2_reduce_total"]["value"] == "4200.5"
    assert points["co2_reduce_total"]["unit"] == "kg"


def test_maps_today_income():
    """today_income becomes a today_income point (#292)."""
    points = map_plant_detail_to_points({"today_income": {"value": "3.50", "unit": "GBP"}})
    assert points["today_income"]["value"] == "3.50"
    assert points["today_income"]["unit"] == "GBP"


def test_maps_total_income():
    """total_income becomes a total_income point (#292)."""
    points = map_plant_detail_to_points({"total_income": {"value": "1850.25", "unit": "GBP"}})
    assert points["total_income"]["value"] == "1850.25"
    assert points["total_income"]["unit"] == "GBP"


# ---------------------------------------------------------------------------
# Missing-unit handling for named plant fields (issue #384)
# ---------------------------------------------------------------------------


def test_named_field_without_unit_still_mapped():
    """A named field with a value but no unit is still surfaced, with an empty unit."""
    points = map_plant_detail_to_points({"curr_power": {"value": "372"}})
    assert points["current_power"]["value"] == "372"
    assert points["current_power"]["unit"] == ""


def test_named_field_missing_unit_warns_once(caplog):
    """The missing unit is logged once per code so the real payload can be captured.

    Without a unit the scale is unknowable (this field is observed as both W and kW),
    so the warning is the only signal that the sensor lost its device class/unit.
    """
    import logging

    from custom_components.sungrow import user_realtime

    user_realtime._WARNED_MISSING_UNIT.clear()
    with caplog.at_level(logging.WARNING, logger=user_realtime.__name__):
        map_plant_detail_to_points({"curr_power": {"value": "372"}})
        map_plant_detail_to_points({"curr_power": {"value": "486"}})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "curr_power" in warnings[0].getMessage()


def test_named_field_with_unit_does_not_warn(caplog):
    """The normal path (unit present) stays silent."""
    import logging

    from custom_components.sungrow import user_realtime

    user_realtime._WARNED_MISSING_UNIT.clear()
    with caplog.at_level(logging.WARNING, logger=user_realtime.__name__):
        map_plant_detail_to_points({"curr_power": {"value": "0.49", "unit": "kW"}})

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
