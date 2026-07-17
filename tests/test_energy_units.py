"""Tests for energy unit normalisation and source tagging."""

from custom_components.sungrow.energy_units import (
    normalize_energy_point,
    normalize_energy_units,
    normalize_power_point,
    normalize_power_units,
    tag_source,
)


def test_wh_to_kwh():
    point = {"code": "total_yield", "value": 6467800.0, "unit": "Wh"}
    out = normalize_energy_point(point)
    assert out["value"] == 6467.8
    assert out["unit"] == "kWh"


def test_kwh_unchanged():
    point = {"code": "total_yield", "value": 6467.8, "unit": "kWh"}
    assert normalize_energy_point(point) is point


def test_payload_and_source_tag():
    data = {
        "total_yield": {"code": "total_yield", "value": 5000, "unit": "Wh"},
        "power": {"code": "power", "value": 100, "unit": "W"},
    }
    norm = normalize_energy_units(data)
    tagged = tag_source(norm, "cloud")
    assert tagged["total_yield"]["value"] == 5.0
    assert tagged["total_yield"]["unit"] == "kWh"
    assert tagged["total_yield"]["source"] == "cloud"
    assert tagged["power"]["source"] == "cloud"
    # Does not overwrite an existing source.
    already = tag_source({"x": {"value": 1, "source": "modbus"}}, "cloud")
    assert already["x"]["source"] == "modbus"


# --- Power normalization (kW → W) for cloud_user path ---


def test_kw_to_w():
    """A kW power point is scaled ×1000 to W."""
    point = {"code": "current_power", "value": "0.49", "unit": "kW"}
    out = normalize_power_point(point)
    assert out["value"] == 490.0
    assert out["unit"] == "W"


def test_kw_to_w_larger_value():
    """Typical residential system: 5.23 kW → 5230 W."""
    point = {"code": "total_active_power", "value": "5.23", "unit": "kW"}
    out = normalize_power_point(point)
    assert out["value"] == 5230.0
    assert out["unit"] == "W"


def test_w_unchanged():
    """A point already in W passes through untouched."""
    point = {"code": "power", "value": "490", "unit": "W"}
    assert normalize_power_point(point) is point


def test_kw_empty_value():
    """A kW point with no value still gets the unit fixed."""
    point = {"code": "current_power", "value": "", "unit": "kW"}
    out = normalize_power_point(point)
    assert out["unit"] == "W"
    assert out["value"] == ""


def test_kw_none_value():
    """A kW point with None value still gets the unit fixed."""
    point = {"code": "current_power", "value": None, "unit": "kW"}
    out = normalize_power_point(point)
    assert out["unit"] == "W"


def test_normalize_power_units_full_payload():
    """normalize_power_units converts all kW points in a payload."""
    data = {
        "current_power": {"code": "current_power", "value": "0.49", "unit": "kW"},
        "total_yield": {"code": "total_yield", "value": "6467.8", "unit": "kWh"},
        "power": {"code": "power", "value": "490", "unit": "W"},
    }
    out = normalize_power_units(data)
    assert out["current_power"]["value"] == 490.0
    assert out["current_power"]["unit"] == "W"
    # Energy units are NOT touched by power normalization.
    assert out["total_yield"]["unit"] == "kWh"
    # Already-W points pass through.
    assert out["power"]["value"] == "490"
    assert out["power"]["unit"] == "W"
