"""Tests for device_helpers nesting helpers."""

from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.device_helpers import build_device_info


def test_build_device_info_defaults_via_to_plant_id():
    """Cloud path: omitting via_plant_id nests under plant_id."""
    info = build_device_info(
        {"uuid": "inv-1", "device_name": "Inv", "device_model_code": "SG3.6RS", "device_sn": "S1"},
        "plant-1",
    )
    assert info["via_device"] == (DOMAIN, "plant-1")


def test_build_device_info_explicit_via_plant_id():
    """Local nested under cloud plant uses the override parent."""
    info = build_device_info(
        {"uuid": "inv-1", "device_name": "Inv"},
        "serial",
        via_plant_id="plant-99",
    )
    assert info["via_device"] == (DOMAIN, "plant-99")


def test_build_device_info_none_via_omits_parent():
    """Explicit via_plant_id=None leaves the device un-nested (no phantom parent)."""
    info = build_device_info(
        {"uuid": "serial_inv", "device_name": "SG3.6RS (local)", "device_sn": "serial"},
        "serial",
        via_plant_id=None,
        configuration_url="http://192.168.1.93",
    )
    assert "via_device" not in info
    assert info["configuration_url"] == "http://192.168.1.93"
    assert info["identifiers"] == {(DOMAIN, "serial_inv")}
