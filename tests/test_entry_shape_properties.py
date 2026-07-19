"""Property-based test for entry data shape per transport mode (#216).

Feature: transport-mode-selector
Property 1: Entry data shape matches transport mode schema
Validates: Requirements 4.1, 4.2, 4.3
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.sungrow.const import (
    CONF_APP_ID,
    CONF_APP_KEY,
    CONF_APP_SECRET,
    CONF_GATEWAY,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_REDIRECT_URI,
    CONF_SERIAL,
    CONF_TRANSPORT,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

CLOUD_FIELDS = {CONF_APP_KEY, CONF_APP_SECRET, CONF_APP_ID, CONF_GATEWAY, CONF_REDIRECT_URI}

# Strategies for generating valid entry data per transport mode
_non_empty_str = st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N")))

cloud_only_strategy = st.fixed_dictionaries(
    {
        CONF_TRANSPORT: st.just(TRANSPORT_CLOUD_ONLY),
        CONF_APP_KEY: _non_empty_str,
        CONF_APP_SECRET: _non_empty_str,
        CONF_APP_ID: _non_empty_str,
        CONF_GATEWAY: st.sampled_from(["Europe", "International", "China", "Australia"]),
        CONF_REDIRECT_URI: _non_empty_str,
        "tokens": st.fixed_dictionaries({"access_token": _non_empty_str, "refresh_token": _non_empty_str}),
    }
)

# The ``cloud_modbus`` shape (cloud fields + modbus_host on one entry) was retired
# in #348 — the transport is no longer selectable, existing entries are migrated
# to ``cloud_only`` (dropping ``modbus_host``) in the v4→v5 migration, and no new
# entries of this shape can be created. Property test removed with the transport.

modbus_only_strategy = st.fixed_dictionaries(
    {
        CONF_TRANSPORT: st.just(TRANSPORT_MODBUS_ONLY),
        CONF_SERIAL: _non_empty_str,
        CONF_MODEL: _non_empty_str,
        CONF_MODBUS_HOST: _non_empty_str,
    }
)


@settings(max_examples=100)
@given(data=cloud_only_strategy)
def test_cloud_only_entry_shape(data: dict):
    """cloud_only entries have all cloud fields, no modbus_host."""
    assert data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    for field in CLOUD_FIELDS:
        assert field in data, f"Missing cloud field: {field}"
    assert CONF_MODBUS_HOST not in data


@settings(max_examples=100)
@given(data=modbus_only_strategy)
def test_modbus_only_entry_shape(data: dict):
    """modbus_only entries have serial, model, modbus_host; no cloud fields."""
    assert data[CONF_TRANSPORT] == TRANSPORT_MODBUS_ONLY
    assert CONF_SERIAL in data
    assert CONF_MODEL in data
    assert CONF_MODBUS_HOST in data
    for field in CLOUD_FIELDS:
        assert field not in data, f"Unexpected cloud field: {field}"
    assert "tokens" not in data
