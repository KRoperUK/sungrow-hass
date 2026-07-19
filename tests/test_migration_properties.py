"""Property-based tests for config entry migration (#216).

Feature: transport-mode-selector
"""

import pytest
from homeassistant.helpers import entity_registry as er
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

# The migration target after every v1/v2/v3 upgrade path (v3→v4 sweeps legacy entities).
CURRENT_VERSION = 4

# ---------------------------------------------------------------------------
# Property 2: v2→v4 migration correctly sets or preserves transport
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
async def test_v2_to_current_migration_sets_or_preserves_transport(
    hass, has_transport: bool, is_modbus: bool, extra_keys: dict
):
    """Property 2: v2→current migration back-fills cloud_only or preserves modbus_only."""
    data = dict(extra_keys)
    if has_transport:
        data[CONF_TRANSPORT] = TRANSPORT_MODBUS_ONLY if is_modbus else TRANSPORT_CLOUD_ONLY

    entry = MockConfigEntry(domain=DOMAIN, data=data, version=2)
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == CURRENT_VERSION

    if has_transport and is_modbus:
        assert entry.data[CONF_TRANSPORT] == TRANSPORT_MODBUS_ONLY
    elif has_transport and not is_modbus:
        assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    else:
        # Was absent → backfilled to cloud_only
        assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY


# ---------------------------------------------------------------------------
# Property 3: v1→current chained migration preserves semantics
# Validates: Requirements 5.5, 5.6
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scan_interval=st.integers(min_value=1, max_value=1440))
@pytest.mark.asyncio
async def test_v1_to_current_chained_migration(hass, scan_interval: int):
    """Property 3: v1 entry migrates end-to-end with correct scan_interval and transport."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"app_key": "k", "app_secret": "s"},
        options={CONF_SCAN_INTERVAL: scan_interval},
        version=1,
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == CURRENT_VERSION
    assert entry.options[CONF_SCAN_INTERVAL] == scan_interval * 60
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY


# ---------------------------------------------------------------------------
# Unit tests for migration edge cases (Task 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_entry_no_transport_gets_cloud_only(hass):
    """v2 entry with no transport field → gets cloud_only, migrated to current version."""
    entry = MockConfigEntry(domain=DOMAIN, data={"app_key": "k"}, version=2)
    entry.add_to_hass(hass)
    await async_migrate_entry(hass, entry)
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY
    assert entry.version == CURRENT_VERSION


@pytest.mark.asyncio
async def test_v2_entry_modbus_only_preserved(hass):
    """v2 entry with modbus_only → kept as modbus_only, migrated to current version."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, "serial": "SN"}, version=2)
    entry.add_to_hass(hass)
    await async_migrate_entry(hass, entry)
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_MODBUS_ONLY
    assert entry.version == CURRENT_VERSION


@pytest.mark.asyncio
async def test_v1_entry_scan_interval_5_migrated(hass):
    """v1 entry with scan_interval=5 → 300s, transport backfilled, migrated to current version."""
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
    assert entry.version == CURRENT_VERSION


@pytest.mark.asyncio
async def test_already_current_no_changes(hass):
    """Already-current entry → no version bump applied."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY, "app_key": "k"}, version=CURRENT_VERSION
    )
    entry.add_to_hass(hass)
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == CURRENT_VERSION
    assert entry.data[CONF_TRANSPORT] == TRANSPORT_CLOUD_ONLY


# ---------------------------------------------------------------------------
# v3→v4: sweep legacy select.*_charge_discharge_command (issue #314)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v3_to_v4_removes_legacy_charge_discharge_select(hass):
    """v3→v4 removes the pre-5.0.0 charge_discharge_command entity from the registry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY, "app_key": "k"},
        version=3,
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    # Legacy entity that v5.0.0 (#255) renamed to select.*_battery_mode.
    legacy = registry.async_get_or_create(
        domain="select",
        platform=DOMAIN,
        unique_id="plant_1_dev_uuid_charge_discharge_command",
        config_entry=entry,
    )
    # A live entity that must be preserved.
    keep = registry.async_get_or_create(
        domain="select",
        platform=DOMAIN,
        unique_id="plant_1_dev_uuid_battery_mode",
        config_entry=entry,
    )
    # An unrelated platform (e.g. a Zigbee entity attached to the same entry via HA
    # weirdness) must not be touched by our platform-scoped sweep.
    foreign = registry.async_get_or_create(
        domain="select",
        platform="zha",
        unique_id="something_charge_discharge_command",
        config_entry=entry,
    )

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 4
    assert registry.async_get(legacy.entity_id) is None
    assert registry.async_get(keep.entity_id) is not None
    assert registry.async_get(foreign.entity_id) is not None


@pytest.mark.asyncio
async def test_v3_to_v4_no_legacy_entities_is_noop(hass):
    """v3→v4 with a clean registry just bumps the version without changes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY, "app_key": "k"},
        version=3,
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 4
