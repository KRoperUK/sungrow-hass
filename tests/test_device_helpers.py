"""Tests for device_helpers nesting helpers."""

from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.device_helpers import build_device_info, build_device_info_for


def test_build_device_info_nests_under_parent_device_id():
    """A cloud device nests under the parent device's registry id."""
    info = build_device_info(
        {"uuid": "inv-1", "device_name": "Inv", "device_model_code": "SG3.6RS", "device_sn": "S1"},
        via_device_id="plant-device-1",
    )
    assert info["via_device_id"] == "plant-device-1"


def test_build_device_info_omits_parent_when_none():
    """A ``None`` parent id leaves the device un-nested (no phantom parent)."""
    info = build_device_info(
        {"uuid": "serial_inv", "device_name": "SG3.6RS (local)", "device_sn": "serial"},
        via_device_id=None,
        configuration_url="http://192.168.1.93",
    )
    assert "via_device_id" not in info
    assert info["configuration_url"] == "http://192.168.1.93"
    assert info["identifiers"] == {(DOMAIN, "serial_inv")}


def test_build_device_info_never_emits_deprecated_via_device():
    """The deprecated ``via_device`` identifier tuple must never be emitted (#407)."""
    info = build_device_info({"uuid": "inv-1"}, via_device_id="plant-device-1")
    assert "via_device" not in info


# ---------------------------------------------------------------------------
# build_device_info_for: transport-aware parentage (issue #383)
# ---------------------------------------------------------------------------


class _Ctx:
    """Minimal stand-in for the coordinator attributes the helper reads."""

    def __init__(self, plant_id, plant_name, local_configuration_url, via_device_id):
        self.plant_id = plant_id
        self.plant_name = plant_name
        self.local_configuration_url = local_configuration_url
        self.via_device_id = via_device_id


_DEVICE = {"uuid": "A22A1574727_inv", "device_name": "SH6.0RT (local)", "device_sn": "A22A1574727"}


def test_build_device_info_for_cloud_nests_under_plant_device():
    """A cloud coordinator nests the device under the plant device registry id."""
    ctx = _Ctx("plant-1", "My Plant", None, "plant-device-1")
    info = build_device_info_for(ctx, {"uuid": "inv-1", "device_name": "Inv"})
    assert info["via_device_id"] == "plant-device-1"


def test_build_device_info_for_local_without_cloud_plant_has_no_parent():
    """Local-only: no parent at all, because plant_id is an unregistered serial.

    Regression for #383: pointing the parent link at ('sungrow', <serial>) makes HA log
    "referencing a non existing via_device" and it will stop being accepted.
    """
    ctx = _Ctx("A22A1574727", "SH6.0RT (local)", "http://10.0.0.5", None)
    info = build_device_info_for(ctx, _DEVICE)
    assert "via_device_id" not in info
    assert info["configuration_url"] == "http://10.0.0.5"


def test_build_device_info_for_local_nests_under_matching_cloud_plant():
    """Local entry whose serial is owned by a cloud plant nests under that plant device."""
    ctx = _Ctx("A22A1574727", "SH6.0RT (local)", "http://10.0.0.5", "cloud-plant-device")
    info = build_device_info_for(ctx, _DEVICE)
    assert info["via_device_id"] == "cloud-plant-device"


def test_build_device_info_for_local_without_host_still_has_no_parent():
    """A hostless local entry is still local: empty string, not None, marks it.

    If ``local_configuration_url`` were left as None the helper would treat it as a cloud
    entry and pick up a parent.
    """
    ctx = _Ctx("A22A1574727", "SH6.0RT (local)", "", None)
    info = build_device_info_for(ctx, _DEVICE)
    assert "via_device_id" not in info
    assert "configuration_url" not in info
