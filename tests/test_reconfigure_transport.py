"""Unit tests for reconfigure flow adaptation per transport mode (#216)."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow.const import (
    CONF_DISCOVERY_MANAGED_HOST,
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)

from .conftest import MOCK_CONFIG_DATA


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession."""
    with patch(
        "custom_components.sungrow._config_flow._base.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# cloud_only reconfigure: shows credentials form
# ---------------------------------------------------------------------------


async def test_reconfigure_cloud_only_shows_credentials(hass: HomeAssistant, mock_auth):
    """A cloud_only entry reconfigure shows the credentials form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        unique_id="test_app_id",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


# cloud_modbus reconfigure was retired in #348; the transport itself is gone
# and existing entries are migrated to cloud_only in migration.py's v4→v5 step.
# The corresponding reconfigure_modbus_host flow is dead — see test_migration_properties.py
# for the migration-path coverage.


# ---------------------------------------------------------------------------
# modbus_only reconfigure: shows host form only
# ---------------------------------------------------------------------------


async def test_reconfigure_modbus_only_shows_host_form(hass: HomeAssistant):
    """A modbus_only entry reconfigure shows the modbus host form only."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "SN123",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "10.0.0.9",
        },
        options={CONF_SCAN_INTERVAL: 30},
        unique_id="modbus_SN123",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure_modbus"
    keys = {str(m.schema) for m in result["data_schema"].schema}
    assert keys == {CONF_MODBUS_HOST}


async def test_reconfigure_modbus_pins_host_and_clears_discovery_managed(hass: HomeAssistant):
    """Setting the host via reconfigure marks it as user-chosen (#402).

    Once the host is pinned, a later WiNet-S discovery must not overwrite it with the
    dongle's address.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "SN123",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "10.0.0.9",
            CONF_DISCOVERY_MANAGED_HOST: True,  # created by discovery
        },
        options={CONF_SCAN_INTERVAL: 30},
        unique_id="modbus_SN123",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure_modbus"

    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "10.0.0.50"}
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MODBUS_HOST] == "10.0.0.50"
    # Pinned: discovery will no longer follow the WiNet-S address for this entry.
    assert entry.data[CONF_DISCOVERY_MANAGED_HOST] is False
