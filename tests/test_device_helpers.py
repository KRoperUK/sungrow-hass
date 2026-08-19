"""Tests for device_helpers nesting helpers."""

from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.device_helpers import build_device_info, build_device_info_for


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


# ---------------------------------------------------------------------------
# build_device_info_for: transport-aware parentage (issue #383)
# ---------------------------------------------------------------------------


class _Ctx:
    """Minimal stand-in for the coordinator attributes the helper reads."""

    def __init__(self, plant_id, plant_name, local_configuration_url, via_plant_id):
        self.plant_id = plant_id
        self.plant_name = plant_name
        self.local_configuration_url = local_configuration_url
        self.via_plant_id = via_plant_id


_DEVICE = {"uuid": "A22A1574727_inv", "device_name": "SH6.0RT (local)", "device_sn": "A22A1574727"}


def test_build_device_info_for_cloud_nests_under_plant():
    """A cloud coordinator (no local url) nests the device under the plant device."""
    ctx = _Ctx("plant-1", "My Plant", None, None)
    info = build_device_info_for(ctx, {"uuid": "inv-1", "device_name": "Inv"})
    assert info["via_device"] == (DOMAIN, "plant-1")


def test_build_device_info_for_local_without_cloud_plant_has_no_parent():
    """Local-only: no via_device at all, because plant_id is an unregistered serial.

    Regression for #383: pointing via_device at ('sungrow', <serial>) makes HA log
    "referencing a non existing via_device" and it will stop being accepted.
    """
    ctx = _Ctx("A22A1574727", "SH6.0RT (local)", "http://10.0.0.5", None)
    info = build_device_info_for(ctx, _DEVICE)
    assert "via_device" not in info
    assert info["configuration_url"] == "http://10.0.0.5"


def test_build_device_info_for_local_nests_under_matching_cloud_plant():
    """Local entry whose serial is owned by a cloud plant nests under that plant."""
    ctx = _Ctx("A22A1574727", "SH6.0RT (local)", "http://10.0.0.5", "plant-99")
    info = build_device_info_for(ctx, _DEVICE)
    assert info["via_device"] == (DOMAIN, "plant-99")


def test_build_device_info_for_local_without_host_still_has_no_parent():
    """A hostless local entry is still local: empty string, not None, marks it.

    If ``local_configuration_url`` were left as None the helper would take the cloud
    branch and re-introduce the phantom via_device.
    """
    ctx = _Ctx("A22A1574727", "SH6.0RT (local)", "", None)
    info = build_device_info_for(ctx, _DEVICE)
    assert "via_device" not in info
    assert "configuration_url" not in info
