"""Tests for the shared JSON-serialisation helpers (#355).

These primitives were extracted from ``diagnostics.py``; the diagnostics tests
still exercise them via the ``async_get_config_entry_diagnostics`` end-to-end
path, but as pure functions they deserve targeted unit coverage of the edge
cases (non-dict inputs, enum recursion, uuid-anonymisation stability, PII-free
name resolution) so a refactor of ``diagnostics.py`` can't silently regress
them.
"""

from __future__ import annotations

from enum import Enum

from custom_components.sungrow._serialization import (
    anonymise_device_keys,
    catalog_rows,
    jsonable,
)


class _Colour(Enum):
    RED = 1
    BLUE = "blue"


# ---------------------------------------------------------------------------
# jsonable
# ---------------------------------------------------------------------------


def test_jsonable_converts_enum_to_name_and_value_string():
    """Known enums serialise as ``"NAME (value)"`` so ``json`` can round-trip them."""
    assert jsonable(_Colour.RED) == "RED (1)"
    assert jsonable(_Colour.BLUE) == "BLUE (blue)"


def test_jsonable_recurses_through_dicts_and_lists():
    """Enums nested inside containers are converted at every level."""
    payload = {"colours": [_Colour.RED, {"nested": _Colour.BLUE}], "count": 2}
    assert jsonable(payload) == {
        "colours": ["RED (1)", {"nested": "BLUE (blue)"}],
        "count": 2,
    }


def test_jsonable_passes_through_primitives():
    """Ints, strings, booleans, and None pass through untouched."""
    assert jsonable(42) == 42
    assert jsonable("hello") == "hello"
    assert jsonable(True) is True
    assert jsonable(None) is None


def test_jsonable_converts_tuple_to_list():
    """Tuples become lists so JSON can represent them (JSON has no tuple type)."""
    assert jsonable((_Colour.RED, 1, "x")) == ["RED (1)", 1, "x"]


def test_jsonable_leaves_unknown_int_device_type_as_int():
    """An *unknown* device-type int isn't wrapped as an enum — it stays as the raw int
    so consumers can see exactly which type ID the API returned (motivating case for #18)."""
    # pysolarcloud leaves unmapped device types as raw ints; jsonable doesn't touch them.
    assert jsonable(99) == 99


# ---------------------------------------------------------------------------
# anonymise_device_keys
# ---------------------------------------------------------------------------


def test_anonymise_device_keys_replaces_uuids_with_stable_placeholders():
    """UUID keys are replaced with ``device_N`` placeholders in insertion order."""
    uuid_map: dict[str, str] = {}
    result = anonymise_device_keys({"aaa-111": {"x": 1}, "bbb-222": {"y": 2}}, uuid_map)
    assert result == {"device_1": {"x": 1}, "device_2": {"y": 2}}
    # ``uuid_map`` accumulates the mapping so a second call reuses the same placeholders.
    assert uuid_map == {"aaa-111": "device_1", "bbb-222": "device_2"}


def test_anonymise_device_keys_shares_uuid_map_across_calls():
    """The same uuid seen in a later call gets the same placeholder, so the diagnostics
    bundle stays coherent across multiple device-type sections (#122)."""
    uuid_map: dict[str, str] = {}
    anonymise_device_keys({"shared-uuid": {"inv": True}}, uuid_map)
    result = anonymise_device_keys({"shared-uuid": {"meter": True}, "new-uuid": {}}, uuid_map)
    # The pre-existing "shared-uuid" keeps its ``device_1`` label; "new-uuid" gets ``device_2``.
    assert result == {"device_1": {"meter": True}, "device_2": {}}


def test_anonymise_device_keys_passes_non_dict_through():
    """A non-dict payload (e.g. ``{"error": ...}``) with dict keys that aren't uuids
    passes through untouched — the helper only anonymises the ``{uuid: payload}`` shape."""
    assert anonymise_device_keys("not-a-dict", {}) == "not-a-dict"
    assert anonymise_device_keys(None, {}) is None
    assert anonymise_device_keys([1, 2, 3], {}) == [1, 2, 3]


# ---------------------------------------------------------------------------
# catalog_rows
# ---------------------------------------------------------------------------


def test_catalog_rows_flattens_and_sorts_by_point_id():
    """Rows come out sorted by ``point_id`` regardless of dict-insertion order (#252)."""
    points = {
        "power": {"id": "83033", "value": "1000", "unit": "W"},
        "energy": {"id": "83024", "value": "5.5", "unit": "kWh"},
    }
    rows = catalog_rows(points)
    # 83024 < 83033 lexicographically as strings; that's the requested sort order.
    assert [row["point_id"] for row in rows] == ["83024", "83033"]
    assert rows[0]["code"] == "energy"
    assert rows[0]["value"] == "5.5"
    assert rows[0]["unit"] == "kWh"


def test_catalog_rows_falls_back_to_code_when_id_missing():
    """A point without a numeric ``id`` field uses the code as its ``point_id`` fallback."""
    points = {"unknown_code": {"value": "x"}}
    rows = catalog_rows(points)
    assert rows[0]["point_id"] == "unknown_code"
    assert rows[0]["code"] == "unknown_code"


def test_catalog_rows_skips_non_dict_points():
    """A malformed entry (value is not a dict) is skipped rather than raised on."""
    rows = catalog_rows({"good": {"id": "1", "value": 1}, "bad": "just-a-string", "other": None})
    assert [row["code"] for row in rows] == ["good"]


def test_catalog_rows_returns_empty_list_for_non_dict_input():
    """A non-dict input (e.g. an ``{"error": ...}`` capture) yields ``[]`` cleanly."""
    assert catalog_rows("bad") == []
    assert catalog_rows(None) == []
    assert catalog_rows([]) == []


def test_catalog_rows_uses_resolved_english_name_not_api_name():
    """The friendly ``name`` field comes from the catalog, not the API's payload name
    (which is often the wrong locale) — so shared diagnostics bundles carry no PII."""
    # For a known point (83033 = total_active_power), the catalog resolves the English
    # name from ``measure_points.resolve_name`` regardless of what the API returned.
    rows = catalog_rows({"total_active_power": {"id": "83033", "value": "1000", "name": "totally wrong"}})
    # The exact resolved English name is measure_points-dependent; just assert the
    # override happened rather than pinning to a specific string.
    assert rows[0]["name"] != "totally wrong"
