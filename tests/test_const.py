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
    expected_regions = {"Europe", "International", "China", "Australia"}
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


def test_meter_device_points_present():
    """The meter map (#179) carries instantaneous power/PF/frequency + per-phase."""
    from custom_components.sungrow.const import METER_DEVICE_POINTS as M

    assert M["8018"] == "meter_active_power"
    assert M["8014"] == "meter_power_factor"
    assert M["8064"] == "meter_frequency"
    assert M["8000"] == "meter_phase_a_voltage"
    assert M["8006"] == "meter_phase_a_current"
