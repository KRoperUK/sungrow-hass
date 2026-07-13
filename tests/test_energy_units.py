"""Tests for energy unit normalisation and source tagging."""

from custom_components.sungrow.energy_units import (
    normalize_energy_point,
    normalize_energy_units,
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
