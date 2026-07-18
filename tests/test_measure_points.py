"""Tests for the Sungrow measuring-point catalog and resolvers."""

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.sungrow import measure_points as mp

M = SensorStateClass.MEASUREMENT
TI = SensorStateClass.TOTAL_INCREASING


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("W", (SensorDeviceClass.POWER, M)),
        ("kWh", (SensorDeviceClass.ENERGY, TI)),
        ("V", (SensorDeviceClass.VOLTAGE, M)),
        ("mV", (SensorDeviceClass.VOLTAGE, M)),
        ("A", (SensorDeviceClass.CURRENT, M)),
        ("Hz", (SensorDeviceClass.FREQUENCY, M)),
        ("°C", (SensorDeviceClass.TEMPERATURE, M)),
        ("var", (SensorDeviceClass.REACTIVE_POWER, M)),
        ("VA", (SensorDeviceClass.APPARENT_POWER, M)),
        # New units for the broader catalogs.
        ("%RH", (SensorDeviceClass.HUMIDITY, M)),
        ("m/s", (SensorDeviceClass.WIND_SPEED, M)),
        ("hPa", (SensorDeviceClass.PRESSURE, M)),
        ("mm", (SensorDeviceClass.PRECIPITATION, M)),
        ("W/m²", (SensorDeviceClass.IRRADIANCE, M)),
        ("h", (SensorDeviceClass.DURATION, M)),
        ("varh", (None, TI)),
        ("kΩ", (None, M)),
        # Case/space-insensitive.
        ("kwh", (SensorDeviceClass.ENERGY, TI)),
        (" W ", (SensorDeviceClass.POWER, M)),
    ],
)
def test_classify_by_unit_known(unit, expected):
    assert mp._classify_by_unit(unit) == expected


@pytest.mark.parametrize("unit", ["", None, "widgets", "%"])
def test_classify_by_unit_unknown_returns_none(unit):
    assert mp._classify_by_unit(unit) is None


# ---------------------------------------------------------------------------
# Enum resolvers
# ---------------------------------------------------------------------------


def test_enum_options_charger_status():
    opts = mp.resolve_enum_options("33716")
    assert opts is not None
    assert "Charging" in opts
    assert "Idle (not plugged in)" in opts
    # Distinct + order-preserving.
    assert len(opts) == len(set(opts))


def test_enum_options_none_for_non_enum():
    assert mp.resolve_enum_options("8018") is None


def test_enum_value_maps_int():
    assert mp.resolve_enum_value("33716", 3) == "Charging"
    assert mp.resolve_enum_value("33716", "3") == "Charging"
    assert mp.resolve_enum_value("33716", 3.0) == "Charging"


def test_enum_value_unmapped_code_returns_none():
    # An unlisted firmware code must NOT leak a value outside _attr_options; HA
    # would reject a state that isn't in the options list (issue #113).
    result = mp.resolve_enum_value("33716", 999)
    assert result is None
    assert not isinstance(result, str)


def test_enum_value_unparseable_returns_none():
    assert mp.resolve_enum_value("33716", "not-a-number") is None


def test_enum_value_none_for_non_enum():
    assert mp.resolve_enum_value("8018", 5) is None


# ---------------------------------------------------------------------------
# Local-Modbus enum decoders (#322)
# ---------------------------------------------------------------------------
# Two register values only make sense as human-readable strings:
#  - ``running_state_raw`` (wire 12999) -> inverter state name
#  - ``device_type_code`` (wire 4999)   -> device model name
# Rather than open-coding decode on the sensor side, these get registered into
# ENUM_MAPS keyed by the Modbus point *code* (Modbus values arrive without a
# numeric point ID, so ``point_id`` falls back to the code — see sensor.py).


def test_running_state_enum_maps_documented_codes():
    """Selected raw states from the Sungrow spec resolve to their documented names."""
    assert mp.resolve_enum_value("running_state_raw", 0x0000) == "Running"
    assert mp.resolve_enum_value("running_state_raw", 0x0008) == "Standby"
    assert mp.resolve_enum_value("running_state_raw", 0x0100) == "Fault"
    assert mp.resolve_enum_value("running_state_raw", 0x2501) == "Restarting"
    assert mp.resolve_enum_value("running_state_raw", 0x4000) == "Running in External EMS mode"


def test_running_state_enum_returns_none_for_unknown_code():
    """An unlisted firmware code must yield None so HA doesn't reject the state."""
    result = mp.resolve_enum_value("running_state_raw", 0xFFFF)
    assert result is None


def test_running_state_enum_classifies_and_exposes_options():
    """``running_state_raw`` is an ENUM point and its options include the common states."""
    assert mp.resolve_classification(None, "running_state_raw", "running_state_raw") == (
        SensorDeviceClass.ENUM,
        None,
    )
    options = mp.resolve_enum_options("running_state_raw")
    assert options is not None
    for expected in ("Running", "Standby", "Fault", "Stop"):
        assert expected in options


def test_device_type_code_enum_maps_known_models():
    """Device-type codes resolve to model-family display names."""
    assert mp.resolve_enum_value("device_type_code", 3355) == "SH10RS"
    assert mp.resolve_enum_value("device_type_code", 3603) == "SH10RT-20"
    assert mp.resolve_enum_value("device_type_code", 9732) == "SG-RS string inverter"


def test_device_type_code_enum_returns_none_for_unknown_code():
    """A new/unknown device-type code must yield None (options list is fixed)."""
    result = mp.resolve_enum_value("device_type_code", 4242)
    assert result is None


def test_every_device_type_family_code_has_a_display_name():
    """Every code we recognise as a family must also have a display name (#322).

    Without this coverage a user with a supported SH inverter would see the raw
    integer in the ``device_type_code`` sensor because it isn't in the options
    list. Regenerating one map without the other would silently regress it.
    """
    from custom_components.sungrow.modbus_registers import (
        DEVICE_MODEL_NAMES,
        DEVICE_TYPE_CODE_TO_FAMILY,
    )

    missing = set(DEVICE_TYPE_CODE_TO_FAMILY) - set(DEVICE_MODEL_NAMES)
    assert not missing, f"family-mapped codes without a model name: {sorted(missing)}"


def test_modbus_enum_codes_get_friendly_names():
    """`Inverter State` and `Device Model` beat the generic title-caser (#322)."""
    assert mp.resolve_name("running_state_raw", "running_state_raw", None) == "Inverter State"
    assert mp.resolve_name("device_type_code", "device_type_code", None) == "Device Model"


@pytest.mark.parametrize("point_id", ["29", "13146"])
def test_operating_status_enum_classifies_and_maps(point_id):
    """The operating-status points (29/13146) are ENUM, map a known code, and drop unknowns.

    Both point IDs share the documented operating-status map. A listed code resolves
    to its human label; an unlisted firmware code must return None rather than leak a
    value outside _attr_options (HA rejects a state not in the options list — #113).
    """
    # Classified as an enum (no state class).
    assert mp.resolve_classification("", "operating_status", point_id) == (SensorDeviceClass.ENUM, None)
    # A documented code maps to its label...
    assert mp.resolve_enum_value(point_id, 0) == "Grid-connected operation"
    # ...and the label is one of the fixed options for the point.
    options = mp.resolve_enum_options(point_id)
    assert options is not None
    assert "Grid-connected operation" in options
    # An unmapped code yields None (not an out-of-options value).
    result = mp.resolve_enum_value(point_id, 40000)
    assert result is None
    assert not isinstance(result, str)


# ---------------------------------------------------------------------------
# resolve_classification
# ---------------------------------------------------------------------------


def test_classify_enum_point():
    assert mp.resolve_classification("", "ev_charger_status", "33716") == (
        SensorDeviceClass.ENUM,
        None,
    )


def test_classify_unit_wins():
    assert mp.resolve_classification("kWh", "anything", "0") == (
        SensorDeviceClass.ENERGY,
        TI,
    )


def test_classify_percent_battery():
    assert mp.resolve_classification("%", "battery_soc", "0") == (SensorDeviceClass.BATTERY, M)


def test_classify_percent_soh_is_not_battery():
    # SOH is health, not charge level — must NOT be BATTERY device class.
    assert mp.resolve_classification("%", "battery_soh", "0") == (None, M)


def test_classify_percent_generic():
    assert mp.resolve_classification("%", "efficiency", "0") == (None, M)


def test_classify_percent_documented_soc_uses_catalog():
    # A documented %-SOC point arriving with an opaque numeric code must still
    # get its BATTERY class from the catalog, not the code-only heuristic (#113).
    assert mp.resolve_classification("%", "24629", "24629") == (SensorDeviceClass.BATTERY, M)


def test_classify_dimensionless_power_factor_by_code():
    assert mp.resolve_classification("", "meter_power_factor", "0") == (
        SensorDeviceClass.POWER_FACTOR,
        M,
    )


def test_classify_dimensionless_soc_by_code():
    assert mp.resolve_classification(None, "total_field_soc", "0") == (SensorDeviceClass.BATTERY, M)


def test_classify_dimensionless_soh_by_code_is_not_battery():
    # An unrecognised, unitless SOH/health code falls through to the code heuristic
    # and must stay numeric-only (measurement), never a BATTERY charge level.
    assert mp.resolve_classification("", "inverter_soh", "999999") == (None, M)


def test_classify_unknown_is_text():
    assert mp.resolve_classification("", "some_status", "0") == (None, None)


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------


def test_catalog_covers_all_device_types():
    ids = {pid for pid, _n, _u in mp.RAW_POINTS}
    # Anchor points from each catalog must be present.
    for pid in ("58604", "33716", "8014", "24629", "13141", "24", "83252", "51301", "2016", "44012"):
        assert pid in ids, f"missing catalog point {pid}"
    # Comprehensive coverage — the union of catalogs is well over 300 points.
    assert len(mp.RAW_POINTS) >= 300


def test_catalog_no_duplicate_ids():
    ids = [pid for pid, _n, _u in mp.RAW_POINTS]
    assert len(ids) == len(set(ids)), "duplicate point IDs in RAW_POINTS"


def test_catalog_entry_names_and_classes():
    info = mp.POINT_CATALOG["58601"]  # Battery Voltage, V
    assert info.name == "Battery Voltage"
    assert info.device_class == SensorDeviceClass.VOLTAGE
    # Dimensionless SOH is numeric, not a battery device class.
    assert mp.POINT_CATALOG["58605"].device_class is None
    assert mp.POINT_CATALOG["58605"].state_class == M
    # Dimensionless power factor.
    assert mp.POINT_CATALOG["8014"].device_class == SensorDeviceClass.POWER_FACTOR
    # Battery level -> battery device class even with no unit.
    assert mp.POINT_CATALOG["58604"].device_class == SensorDeviceClass.BATTERY


def test_enum_maps_reference_real_catalog_points():
    # Cloud enum points must exist in the transcribed iSolarCloud catalog so
    # sensor entities pick up their documented options. Local-Modbus enum keys
    # (#322) are code-string keys (``running_state_raw``, ``device_type_code``)
    # decoded from register maps and deliberately have no catalog entry — check
    # only the numeric cloud IDs.
    from custom_components.sungrow.modbus_registers import MODBUS_ENUM_MAPS

    for pid in mp.ENUM_MAPS:
        if pid in MODBUS_ENUM_MAPS:
            continue
        assert pid in mp.POINT_CATALOG, f"enum point {pid} not in catalog"
        assert mp.POINT_CATALOG[pid].options, f"enum point {pid} missing options"


def test_classify_uses_catalog_fallback_for_unitless():
    # No unit passed at runtime, but the catalog knows 8014 is a power factor.
    assert mp.resolve_classification(None, "8014", "8014") == (SensorDeviceClass.POWER_FACTOR, M)


# ---------------------------------------------------------------------------
# Per-point classification overrides (issue #113)
# ---------------------------------------------------------------------------

ES = SensorDeviceClass.ENERGY_STORAGE
DURATION = SensorDeviceClass.DURATION


@pytest.mark.parametrize(
    "point_id",
    [
        "83235",  # Total field chargeable energy
        "83236",  # Total field dischargeable energy
        "24630",  # Energy Storage Remaining Charge
        "83327",  # Energy Storage Remaining Charge
        "13140",  # Battery Capacity
    ],
)
def test_stored_energy_points_are_energy_storage_measurement(point_id):
    # Instantaneous stored-energy Wh gauges go up AND down: ENERGY_STORAGE /
    # MEASUREMENT so drops aren't read as meter resets (#113).
    assert mp.POINT_CATALOG[point_id].device_class == ES
    assert mp.POINT_CATALOG[point_id].state_class == M
    # The override must beat the unit map at runtime too (live unit is "Wh").
    assert mp.resolve_classification("Wh", "anything", point_id) == (ES, M)


@pytest.mark.parametrize("point_id", ["58606", "13034"])
def test_cumulative_energy_stays_total_increasing(point_id):
    # Genuinely cumulative "Total ... charge energy" counters must stay
    # ENERGY / TOTAL_INCREASING.
    assert mp.POINT_CATALOG[point_id].device_class == SensorDeviceClass.ENERGY
    assert mp.POINT_CATALOG[point_id].state_class == TI
    assert mp.resolve_classification("Wh", "anything", point_id) == (SensorDeviceClass.ENERGY, TI)


@pytest.mark.parametrize(
    "point_id",
    [
        "13020",  # Total Operation Time
        "7356",  # Total Operation Time
        "3",  # Total On-grid Running Time
        "13016",  # Total Charging Time
        "13017",  # Total Discharging Time
    ],
)
def test_total_hours_counters_are_total_increasing(point_id):
    # Monotonic "Total ... Time" hour counters accumulate: DURATION / TOTAL_INCREASING.
    assert mp.POINT_CATALOG[point_id].device_class == DURATION
    assert mp.POINT_CATALOG[point_id].state_class == TI
    assert mp.resolve_classification("h", "anything", point_id) == (DURATION, TI)


@pytest.mark.parametrize("point_id", ["13023", "13024", "83005", "83025"])
def test_daily_and_reset_hour_timers_stay_measurement(point_id):
    # Daily / reset / equivalent-hour timers are instantaneous: DURATION / MEASUREMENT.
    assert mp.POINT_CATALOG[point_id].device_class == DURATION
    assert mp.POINT_CATALOG[point_id].state_class == M


@pytest.mark.parametrize("point_id", ["83019", "83419"])
def test_capacity_ratio_points_are_numeric_measurement(point_id):
    # "X / Installed Y" ratios arrive as a bare 0–1 fraction with no unit. Classify
    # them numeric (None, MEASUREMENT) so they coerce to a float and graph instead of
    # becoming a text sensor; the sensor platform presents them as a percentage.
    assert point_id in mp.PERCENT_FRACTION_POINT_IDS
    assert mp.POINT_CATALOG[point_id].device_class is None
    assert mp.POINT_CATALOG[point_id].state_class == M
    # The override wins whether or not the API sends a (blank) unit at runtime.
    assert mp.resolve_classification("", "power_fraction", point_id) == (None, M)
    assert mp.resolve_classification(None, "power_fraction", point_id) == (None, M)


# ---------------------------------------------------------------------------
# resolve_name
# ---------------------------------------------------------------------------


def test_resolve_name_alias_wins():
    assert mp.resolve_name("83240", "total_field_power_factor", "PF-cn") == "Battery Power Factor"


def test_resolve_name_numeric_code_uses_catalog():
    # Opaque numeric code -> English catalog name, not the (often Chinese) API name.
    assert mp.resolve_name("58601", "58601", "电池电压") == "Battery Voltage"


def test_resolve_name_readable_code_title_cases():
    assert mp.resolve_name("0", "total_active_power", None) == "Total Active Power"


def test_resolve_name_unknown_numeric_falls_back():
    assert mp.resolve_name("99999", "99999", None) == "Sensor 99999"
    assert mp.resolve_name("99999", "99999", "Some API Name") == "Some API Name"


def test_resolve_name_83123_total_feed_in_energy():
    """Plant point 83123 is total feed-in on the user-cloud path (#281)."""
    assert mp.resolve_name("83123", "83123", None) == "Total Feed-in Energy (PV)"
    assert "83123" in mp.POINT_CATALOG
    info = mp.POINT_CATALOG["83123"]
    assert info.name == "Total Feed-in Energy (PV)"
