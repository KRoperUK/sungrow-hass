"""Cross-entry ``unique_id`` collision handling for the dispatch/sensor platforms.

When a user configures more than one Sungrow entry that covers the same iSolarCloud
plant (duplicate accounts, migration in progress, ...), each entry's platform setup
tries to add entities with the same ``unique_id``. HA's entity registry keys on
``(platform, domain, unique_id)`` so the second attempt is rejected with an
``ERROR`` level ``Platform sungrow does not generate unique IDs`` log per entity,
per coordinator tick.

The platforms filter these out in their ``_add_new_entities`` closures via
:func:`custom_components.sungrow.device_helpers.unique_id_owned_by_other_entry`
and log a single ``INFO`` line instead. These tests cover both the helper and each
platform's use of it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow import SungrowData
from custom_components.sungrow.binary_sensor import async_setup_entry as binary_sensor_setup_entry
from custom_components.sungrow.const import DOMAIN
from custom_components.sungrow.device_helpers import unique_id_owned_by_other_entry
from custom_components.sungrow.number import async_setup_entry as number_setup_entry
from custom_components.sungrow.select import async_setup_entry as select_setup_entry

from .conftest import MOCK_CONFIG_DATA

# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


async def test_unique_id_owned_by_other_entry_no_existing_returns_false(hass: HomeAssistant):
    """No existing entry: the helper returns False so the caller adds the entity."""
    assert not unique_id_owned_by_other_entry(hass, "number", "12345_dev_charge_discharge_power", "entry-a")


async def test_unique_id_owned_by_other_entry_same_entry_returns_false(hass: HomeAssistant):
    """The current entry owning the id is not a collision — the caller adds normally."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy())
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("number", DOMAIN, "12345_dev_charge_discharge_power", config_entry=entry)

    assert not unique_id_owned_by_other_entry(hass, "number", "12345_dev_charge_discharge_power", entry.entry_id)


async def test_unique_id_owned_by_other_entry_other_entry_returns_true(hass: HomeAssistant):
    """A different entry already owning the id returns True — the caller skips silently."""
    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), title="A")
    entry_a.add_to_hass(hass)
    entry_b = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), title="B")
    entry_b.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("number", DOMAIN, "12345_dev_charge_discharge_power", config_entry=entry_a)

    assert unique_id_owned_by_other_entry(hass, "number", "12345_dev_charge_discharge_power", entry_b.entry_id)


async def test_unique_id_owned_by_other_entry_orphan_returns_false(hass: HomeAssistant):
    """An entity with no ``config_entry_id`` (orphaned) does not count as a foreign owner.

    Users can end up with orphaned entries after a delete-without-clean flow; treating
    them as foreign would prevent the entity from ever being re-created after cleanup.
    """
    registry = er.async_get(hass)
    orphan = registry.async_get_or_create("number", DOMAIN, "12345_dev_charge_discharge_power")
    assert orphan.config_entry_id is None
    assert not unique_id_owned_by_other_entry(hass, "number", "12345_dev_charge_discharge_power", "entry-b")


async def test_unique_id_owned_by_other_entry_scoped_by_platform(hass: HomeAssistant):
    """A collision on ``select`` doesn't shadow a fresh ``number`` unique_id (platform scoping)."""
    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), title="A")
    entry_a.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("select", DOMAIN, "12345_dev_battery_mode", config_entry=entry_a)

    # Same unique_id, different platform — no cross-platform collision.
    assert not unique_id_owned_by_other_entry(hass, "number", "12345_dev_battery_mode", "entry-b")


# ---------------------------------------------------------------------------
# Platform integration tests: a second entry with the same coordinator target
# device must not attempt to add already-registered entities.
# ---------------------------------------------------------------------------


def _make_coordinator(plant_id: str, devices: list[dict], *, has_battery: bool = True):
    coordinator = MagicMock()
    coordinator.plant_id = plant_id
    coordinator.plant_name = "Test Plant"
    coordinator.config_entry = MagicMock()
    coordinator.last_update_success = True
    coordinator.has_battery = has_battery
    coordinator.forced_dispatch_duration_minutes = 0
    coordinator.devices = devices
    coordinator.dispatch_update_supported = True
    coordinator.plants_service = MagicMock()  # cloud path for binary sensors
    coordinator.data = {}
    return coordinator


def _seed_runtime_data(entry, devices, *, plant_id: str = "12345") -> None:
    control = MagicMock()
    control.async_update_parameters = AsyncMock(return_value=[])
    control.async_heartbeat = AsyncMock()
    coordinator = _make_coordinator(plant_id, devices)
    entry.runtime_data = SungrowData(
        coordinators=[coordinator],
        control=control,
        devices={plant_id: devices},
    )


@pytest.mark.parametrize(
    ("platform", "setup_entry"),
    [
        ("number", number_setup_entry),
        ("select", select_setup_entry),
        ("binary_sensor", binary_sensor_setup_entry),
    ],
)
async def test_second_entry_skips_entities_owned_by_first_entry(
    hass: HomeAssistant, platform: str, setup_entry
) -> None:
    """A second entry on the same plant does not attempt to add already-owned entities.

    Sets up entry A, records its unique_ids in the entity registry (owned by A), then
    sets up entry B against the *same* coordinator target and asserts B produces no new
    entities — the collision path is skipped silently rather than surfaced as an ERROR
    from HA core's ``does not generate unique IDs`` guard.
    """
    devices = [{"uuid": "dev-uuid-1", "device_type": "ENERGY_STORAGE_SYSTEM", "device_name": "Inverter 1"}]

    entry_a = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), title="A")
    entry_a.add_to_hass(hass)
    _seed_runtime_data(entry_a, devices)

    added_a: list = []
    await setup_entry(hass, entry_a, lambda entities: added_a.extend(entities))
    assert added_a, f"{platform} entry A should create entities for the ESS device"

    # Register A's unique_ids in the entity registry against entry A. Real HA setup does
    # this via async_add_entities → EntityPlatform.async_add_entities; going through the
    # entity registry directly keeps this a focused unit-scope test.
    registry = er.async_get(hass)
    for entity in added_a:
        if entity.unique_id is None:
            continue
        registry.async_get_or_create(platform, DOMAIN, entity.unique_id, config_entry=entry_a)

    entry_b = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_DATA.copy(), title="B")
    entry_b.add_to_hass(hass)
    _seed_runtime_data(entry_b, devices)

    added_b: list = []
    await setup_entry(hass, entry_b, lambda entities: added_b.extend(entities))
    assert added_b == [], (
        f"{platform} entry B should skip every unique_id already owned by entry A "
        f"but was asked to add {[getattr(e, 'unique_id', None) for e in added_b]}"
    )
