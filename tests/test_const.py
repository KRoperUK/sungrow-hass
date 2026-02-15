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
