"""Tests for the user-account getPsDetail -> measure-point mapper (#269)."""

from custom_components.sungrow.measure_points import resolve_name
from custom_components.sungrow.user_realtime import (
    map_device_list_to_points,
    map_plant_detail_to_points,
)


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


def test_current_power_without_unit_falls_back_to_watts():
    """curr_power with no unit falls back to W so it still classifies as power (#384).

    getPsDetailWithPsType (lib 0.16.0) pairs curr_power with a unit; on the rare payload
    that omits it, W is the safe base unit (the app reads curr_power/curr_power_unit
    together, observed as W). normalize_power_units still rescales a real kW reading.
    """
    points = map_plant_detail_to_points({"curr_power": {"value": "372"}})
    assert points["current_power"]["value"] == "372"
    assert points["current_power"]["unit"] == "W"


def test_current_power_unit_fallback_classifies_as_power():
    """The W fallback makes the sensor a proper power sensor (device+state class) (#384)."""
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    from custom_components.sungrow.measure_points import resolve_classification

    point = map_plant_detail_to_points({"curr_power": {"value": "372"}})["current_power"]
    assert resolve_classification(point["unit"], point["code"], point["id"]) == (
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    )


def test_current_power_missing_unit_does_not_warn(caplog):
    """The power fallback is a silent debug, not a warning (#384)."""
    import logging

    from custom_components.sungrow import user_realtime

    user_realtime._WARNED_MISSING_UNIT.clear()
    with caplog.at_level(logging.WARNING, logger=user_realtime.__name__):
        map_plant_detail_to_points({"curr_power": {"value": "372"}})

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_energy_field_missing_unit_still_warns_once(caplog):
    """A field with no safe fallback (energy could be Wh or kWh) still warns once (#384).

    Only power has a safe base-unit fallback; energy fields keep the unit-less state and
    surface the missing unit so the real payload can be captured.
    """
    import logging

    from custom_components.sungrow import user_realtime

    user_realtime._WARNED_MISSING_UNIT.clear()
    with caplog.at_level(logging.WARNING, logger=user_realtime.__name__):
        map_plant_detail_to_points({"today_energy": {"value": "12.5"}})
        map_plant_detail_to_points({"today_energy": {"value": "13.0"}})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "today_energy" in warnings[0].getMessage()
    # The value keeps an empty unit (scale unknowable), unlike the power fallback.
    assert map_plant_detail_to_points({"today_energy": {"value": "12.5"}})["today_energy"]["unit"] == ""


def test_named_field_with_unit_does_not_warn(caplog):
    """The normal path (unit present) stays silent."""
    import logging

    from custom_components.sungrow import user_realtime

    user_realtime._WARNED_MISSING_UNIT.clear()
    with caplog.at_level(logging.WARNING, logger=user_realtime.__name__):
        map_plant_detail_to_points({"curr_power": {"value": "0.49", "unit": "kW"}})

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# ---------------------------------------------------------------------------
# Per-device points embedded in the user-API device list (issue #389)
# ---------------------------------------------------------------------------

# Shape taken from the #389 report: an SBR096 battery (device_type 43) whose
# Battery SOC (58604) the API returns but which produced no entity.
_SBR_DEVICE = {
    "uuid": "dev-battery-1",
    "device_type": 43,
    "type_name": "Battery",
    "device_name": "SBR096",
    "point_data": [
        {
            "point_id": 58604,
            "point_name": "Battery SOC",
            "unit": "%",
            "value": "32.2",
            "raw_value": "0.322",
            "original_value": "32.2",
        },
        {"point_id": 58601, "point_name": "Battery Voltage", "unit": "V", "value": "290.6"},
        {"point_id": 58603, "point_name": "Battery Temperature", "unit": "℃", "value": "22.6"},
    ],
}


def test_device_list_points_are_mapped_by_uuid():
    """Each device's embedded point_data becomes per-device realtime keyed by uuid."""
    out = map_device_list_to_points([_SBR_DEVICE])
    assert set(out) == {"dev-battery-1"}
    soc = out["dev-battery-1"]["58604"]
    assert soc == {
        "id": "58604",
        "code": "58604",
        "value": "32.2",
        "unit": "%",
        "name": "Battery SOC",
    }


def test_battery_soc_resolves_to_catalog_name_and_class():
    """The bare numeric code takes the catalog path, matching the OAuth transport.

    This is the actual regression in #389: point 58604 was returned by the API and
    visible in diagnostics but never reached the resolver, so no entity was created.
    """
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    from custom_components.sungrow.measure_points import resolve_classification

    point = map_device_list_to_points([_SBR_DEVICE])["dev-battery-1"]["58604"]
    assert resolve_name(point["id"], point["code"], point["name"]) == "Battery Level"
    assert resolve_classification(point["unit"], point["code"], point["id"]) == (
        SensorDeviceClass.BATTERY,
        SensorStateClass.MEASUREMENT,
    )


def test_device_list_skips_placeholder_values():
    """Points the device does not report are dropped rather than becoming Unknown."""
    device = {
        "uuid": "d1",
        "point_data": [
            {"point_id": 1, "value": "--", "unit": "%"},
            {"point_id": 2, "value": "", "unit": "%"},
            {"point_id": 3, "value": None, "unit": "%"},
            {"point_id": 4, "value": "5.5", "unit": "%"},
        ],
    }
    points = map_device_list_to_points([device])["d1"]
    assert set(points) == {"4"}


def test_device_list_ignores_devices_without_point_data():
    """A device with no embedded points contributes nothing (no empty dict entry)."""
    assert map_device_list_to_points([{"uuid": "d1", "device_type": 1}]) == {}
    assert map_device_list_to_points([{"uuid": "d1", "point_data": []}]) == {}


def test_device_list_tolerates_malformed_entries():
    """Non-dict devices/points and missing uuid/point_id are skipped, not raised."""
    devices = [
        "not-a-dict",
        {"device_type": 1, "point_data": [{"point_id": 1, "value": "1"}]},  # no uuid
        {"uuid": "d2", "point_data": ["nope", {"value": "1"}, {"point_id": 9, "value": "2"}]},
    ]
    assert map_device_list_to_points(devices) == {  # type: ignore[arg-type]
        "d2": {"9": {"id": "9", "code": "9", "value": "2", "unit": "", "name": None}}
    }
