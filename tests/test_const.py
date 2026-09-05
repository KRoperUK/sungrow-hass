"""Tests for constants module."""

from custom_components.sungrow.const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_REDIRECT_URI,
    DOMAIN,
    GATEWAYS,
)


def test_domain():
    """Test the domain constant."""
    assert DOMAIN == "sungrow"


def test_gateways_all_https():
    """Test all gateway URLs use HTTPS."""
    for name, url in GATEWAYS.items():
        assert url.startswith("https://"), f"Gateway {name} does not use HTTPS: {url}"


def test_gateways_expected_regions():
    """Test expected gateway regions are present."""
    expected_regions = {"Europe", "International", "China", "Australia", "India"}
    assert set(GATEWAYS.keys()) == expected_regions


def test_gateway_urls_are_unique():
    """Test all gateway URLs are unique."""
    urls = list(GATEWAYS.values())
    assert len(urls) == len(set(urls)), "Duplicate gateway URLs found"


def test_config_key_names():
    """Test config keys haven't changed unexpectedly."""
    assert CONF_APP_KEY == "app_key"
    assert CONF_APP_SECRET == "app_secret"
    assert CONF_APP_ID == "app_id"
    assert CONF_GATEWAY == "gateway"
    assert CONF_REDIRECT_URI == "redirect_uri"


def test_inverter_diagnostic_points_include_grid_health():
    """The inverter diagnostic map carries the #179 grid-side health points."""
    from custom_components.sungrow.const import INVERTER_DIAGNOSTIC_POINTS as P

    expected = {
        "3": "total_running_time",
        "18": "phase_a_voltage",
        "21": "phase_a_current",
        "25": "reactive_power",
        "26": "power_factor",
        "95": "bus_voltage",
    }
    for pid, code in expected.items():
        assert P[pid] == code
    # The pre-existing MPPT/operating-status points remain.
    assert P["29"] == "operating_status"


def test_inverter_diagnostic_points_include_per_string():
    """The inverter map carries per-string DC voltage/current (#189)."""
    from custom_components.sungrow.const import INVERTER_DIAGNOSTIC_POINTS as P

    # Strings 1-8: voltage 96-103, current 70-77 (live-confirmed for strings 1-2).
    assert P["96"] == "string_1_voltage"
    assert P["70"] == "string_1_current"
    assert P["97"] == "string_2_voltage"
    assert P["103"] == "string_8_voltage"
    assert P["77"] == "string_8_current"
    string_codes = [c for c in P.values() if c.startswith("string_")]
    assert len(string_codes) == 16  # 8 strings x (voltage + current)


def test_ess_mppt_diagnostic_points_shape():
    """The hybrid/ESS MPPT map (this fix) maps the 13xxx IDs to the reused mpptN_* codes.

    SH-family hybrids report MPPT voltage/current on a separate point-ID range than string
    inverters; the codes are reused so classification/naming/icons stay identical.
    """
    from custom_components.sungrow.const import ESS_MPPT_DIAGNOSTIC_POINTS as P

    expected = {
        "13001": "mppt1_voltage",
        "13002": "mppt1_current",
        "13105": "mppt2_voltage",
        "13106": "mppt2_current",
        "13107": "mppt3_voltage",
        "13108": "mppt3_current",
        "13109": "mppt4_voltage",
        "13110": "mppt4_current",
    }
    assert expected == P
    # Exactly MPPT1-4 voltage/current (eight points), reusing the string-inverter code names.
    assert len(P) == 8
    mppt_codes = [c for c in P.values() if c.startswith("mppt")]
    assert len(mppt_codes) == 8


def test_ess_mppt_codes_reuse_string_inverter_codes():
    """MPPT1-3 codes are shared with the string-inverter map; only mppt4_* are new.

    Reusing the existing mppt1-3 code names is what keeps the diagnostic classification,
    naming and icons identical without any extra mapping.
    """
    from custom_components.sungrow.const import ESS_MPPT_DIAGNOSTIC_POINTS as ESS
    from custom_components.sungrow.const import INVERTER_DIAGNOSTIC_POINTS as INV

    inv_codes = set(INV.values())
    # mppt1-3 codes already exist on the string-inverter map (via "5"-"10").
    for code in ("mppt1_voltage", "mppt1_current", "mppt2_voltage", "mppt3_current"):
        assert code in inv_codes
    # mppt4_* are the only genuinely new code names introduced by this map.
    new_codes = set(ESS.values()) - inv_codes
    assert new_codes == {"mppt4_voltage", "mppt4_current"}


def test_meter_device_points_present():
    """The meter map (#179) carries instantaneous power/PF/frequency + per-phase."""
    from custom_components.sungrow.const import METER_DEVICE_POINTS as M

    assert M["8018"] == "meter_active_power"
    assert M["8014"] == "meter_power_factor"
    assert M["8064"] == "meter_frequency"
    assert M["8000"] == "meter_phase_a_voltage"
    assert M["8006"] == "meter_phase_a_current"


def test_battery_points_include_cell_health():
    """The battery map (#180) carries cell/module-level health points, all diagnostic."""
    from custom_components.sungrow.const import BATTERY_DEVICE_POINTS as P
    from custom_components.sungrow.const import BATTERY_DIAGNOSTIC_CODES as D

    expected = {
        "58610": "battery_max_cell_voltage",
        "58612": "battery_min_cell_voltage",
        "58614": "battery_max_module_temperature",
        "58616": "battery_min_module_temperature",
        "58608": "battery_operation_status",
        "58635": "battery_dc_contactor_status",
        "58636": "battery_fault_module_id",
    }
    for pid, code in expected.items():
        assert P[pid] == code
        assert code in D  # health points are Diagnostic, not primary
    # The pre-existing primary points (SOC, energy) remain and stay primary.
    assert P["58604"] == "battery_level"
    assert "battery_level" not in D
