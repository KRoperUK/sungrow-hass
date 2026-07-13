"""Property-based tests for config entry migration (#216).

Feature: transport-mode-selector
"""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import async_migrate_entry
from custom_components.sungrow.const import (
    CONF_SCAN_INTERVAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

# ---------------------------------------------------------------------------
# Property 2: v2→v3 migration correctly sets or preserves transport
# Validates: Requirements 5.2, 5.3, 5.4
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    has_transport=st.booleans(),
    is_modbus=st.booleans(),
    extra_keys=st.dictionaries(
        keys=st.text(min_size=1, max_size=20).filter(lambda k: k != "transport"),
        values=st.text(min_size=1, max_size=50),
        max_size=5,
    ),
)
@pytest.mark.asyncio
async def test_v2_to_v3_migration_sets_or_preserves_transport(
    hass, has_transport: bool, is_modbus: bool, extra_keys: dict
):
    """Property 2: v2→v3 migration back-fills cloud_only or preserves modbus_only."""
    data = dict(extra_keys)
    if has_transport:
        data[CONF_TRANSPORT] = TRANSPORT_MODBUS_ONLY if is_modbus else TRANSPORT_CLOUD_ONLY

    entry = MockConfigEntry(domain=DOMAIN, data=data, version=2)
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 3

    if has_transport and is_modbus:
        assert entry.data[CONF_TRANSPORT] == TRANSPORT_MODBUS_ONLY
    elif has_transport and not is_modbus:
        assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    else:
        # Was absent → backfilled to cloud_only
        assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY


# ---------------------------------------------------------------------------
# Property 3: v1→v3 chained migration preserves semantics
# Validates: Requirements 5.5, 5.6
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scan_interval=st.integers(min_value=1, max_value=1440))
@pytest.mark.asyncio
async def test_v1_to_v3_chained_migration(hass, scan_interval: int):
    """Property 3: v1 entry migrates to v3 with correct scan_interval and transport."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"app_key": "k", "app_secret": "s"},
        options={CONF_SCAN_INTERVAL: scan_interval},
        version=1,
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 3
    assert entry.options[CONF_SCAN_INTERVAL] == scan_interval * 60
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY


# ---------------------------------------------------------------------------
# Unit tests for migration edge cases (Task 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_entry_no_transport_gets_cloud_only(hass):
    """v2 entry with no transport field → gets cloud_only, version 3."""
    entry = MockConfigEntry(domain=DOMAIN, data={"app_key": "k"}, version=2)
    entry.add_to_hass(hass)
    await async_migrate_entry(hass, entry)
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    assert entry.version == 3


@pytest.mark.asyncio
async def test_v2_entry_modbus_only_preserved(hass):
    """v2 entry with modbus_only → kept as modbus_only, version 3."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, "serial": "SN"}, version=2)
    entry.add_to_hass(hass)
    await async_migrate_entry(hass, entry)
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_MODBUS_ONLY
    assert entry.version == 3


@pytest.mark.asyncio
async def test_v1_entry_scan_interval_5_migrated(hass):
    """v1 entry with scan_interval=5 → 300s, transport backfilled, version 3."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"app_key": "k"},
        options={CONF_SCAN_INTERVAL: 5},
        version=1,
    )
    entry.add_to_hass(hass)
    await async_migrate_entry(hass, entry)
    assert entry.options[CONF_SCAN_INTERVAL] == 300
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    assert entry.version == 3


@pytest.mark.asyncio
async def test_already_v3_no_changes(hass):
    """Already-v3 entry → no changes applied."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY, "app_key": "k"}, version=3)
    entry.add_to_hass(hass)
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 3
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
