"""Unit tests for the guided local-Modbus setup wizard (#374).

The wizard is a chain of four steps:

  local_discovery → local_manual_ip → local_confirm_identified → CREATE_ENTRY
                                    ↘ local_setup (manual fallback)

Each test drives the flow starting from the transport selector, then patches out
the network-facing helpers (zeroconf discovery, TCP reachability, Modbus identity
read) to assert the branching behaviour without touching real hardware.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.sungrow._config_flow._helpers import WinetDongle
from custom_components.sungrow.const import (
    CONF_MODBUS_HOST,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DEFAULT_MODBUS_SCAN_INTERVAL,
    DOMAIN,
    TRANSPORT_MODBUS_ONLY,
)


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession so no real ClientSession is created for the OAuth path."""
    with patch(
        "custom_components.sungrow._config_flow._base.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


async def _start_local_flow(hass: HomeAssistant, dongles: list[WinetDongle]) -> str:
    """Kick the user through the transport selector into local_discovery, return flow_id."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.sungrow._config_flow.modbus_only.async_discover_winet_dongles",
        return_value=dongles,
    ):
        step = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY}
        )
    assert step["step_id"] == "local_discovery"
    return result["flow_id"]


# ---------------------------------------------------------------------------
# Discovery step
# ---------------------------------------------------------------------------


async def test_discovery_lists_dongles_in_picker(hass: HomeAssistant):
    """A discovered WiNet-S appears in the choice selector alongside the two synthetic actions."""
    dongle = WinetDongle(host="192.168.1.42", serial="A12345", model="SH10RT", mdns_name="SH10RT._http._tcp.local.")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.sungrow._config_flow.modbus_only.async_discover_winet_dongles",
        return_value=[dongle],
    ):
        step = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY}
        )
    assert step["step_id"] == "local_discovery"
    options = {opt["value"] for opt in step["data_schema"].schema["choice"].config["options"]}
    assert options == {"192.168.1.42", "manual_ip", "rescan"}
    assert step["description_placeholders"] == {"count": "1"}


async def test_discovery_picking_dongle_goes_to_confirm(hass: HomeAssistant):
    """Selecting a discovered dongle in the picker jumps straight to the confirm step."""
    dongle = WinetDongle(host="192.168.1.42", serial="A12345", model="SH10RT", mdns_name=None)
    flow_id = await _start_local_flow(hass, [dongle])
    step = await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "192.168.1.42"})
    assert step["step_id"] == "local_confirm_identified"
    assert step["description_placeholders"] == {"host": "192.168.1.42", "model": "SH10RT", "serial": "A12345"}


async def test_discovery_rescan_reruns_the_scan(hass: HomeAssistant):
    """The Rescan action triggers a fresh discovery browse and re-renders the picker."""
    flow_id = await _start_local_flow(hass, [])
    with patch(
        "custom_components.sungrow._config_flow.modbus_only.async_discover_winet_dongles",
        return_value=[WinetDongle(host="10.0.0.7", serial="A9", model="SH5.0RT", mdns_name=None)],
    ) as m:
        step = await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "rescan"})
    assert m.called
    assert step["step_id"] == "local_discovery"
    assert step["description_placeholders"] == {"count": "1"}


# ---------------------------------------------------------------------------
# Manual-IP step
# ---------------------------------------------------------------------------


async def test_manual_ip_happy_path_identifies_and_creates(hass: HomeAssistant):
    """Manual IP → reachable + full identify → confirm → CREATE_ENTRY (#374 happy path)."""
    flow_id = await _start_local_flow(hass, [])
    await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "manual_ip"})

    with (
        patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=True),
        patch(
            "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity",
            return_value=("SG3.6RS", "SN-HAPPY"),
        ),
    ):
        confirm = await hass.config_entries.flow.async_configure(flow_id, user_input={CONF_MODBUS_HOST: "10.1.2.3"})
    assert confirm["step_id"] == "local_confirm_identified"

    with (
        patch(
            "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity",
            return_value=("SG3.6RS", "SN-HAPPY"),
        ),
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        created = await hass.config_entries.flow.async_configure(flow_id, user_input={})
        await hass.async_block_till_done()

    assert created["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert created["data"] == {
        CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
        CONF_MODBUS_HOST: "10.1.2.3",
        CONF_SERIAL: "SN-HAPPY",
        CONF_MODEL: "SG3.6RS",
    }
    assert created["options"] == {CONF_SCAN_INTERVAL: DEFAULT_MODBUS_SCAN_INTERVAL}
    assert created["result"].unique_id == "modbus_SN-HAPPY"


async def test_manual_ip_unreachable_stays_on_form(hass: HomeAssistant):
    """A failed reachability probe keeps the user on the manual-IP form with an error."""
    flow_id = await _start_local_flow(hass, [])
    await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "manual_ip"})

    with patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=False):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.99"})
    assert result["step_id"] == "local_manual_ip"
    assert result["errors"] == {"base": "host_unreachable"}


async def test_identify_partial_falls_back_to_manual_prefilled(hass: HomeAssistant):
    """Reachable host + only the serial identified → fallback manual form pre-filled."""
    flow_id = await _start_local_flow(hass, [])
    await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "manual_ip"})

    with (
        patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=True),
        patch(
            "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity",
            return_value=(None, "SN-PARTIAL"),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.5"})
    assert result["step_id"] == "local_setup"
    # Pre-filled defaults carry the reachable host + the serial we did learn; model is the
    # generic placeholder because identify returned None for it.
    defaults = {marker.schema: marker.default() for marker in result["data_schema"].schema}
    assert defaults[CONF_MODBUS_HOST] == "10.0.0.5"
    assert defaults[CONF_SERIAL] == "SN-PARTIAL"
    assert defaults[CONF_MODEL] == "Inverter"


async def test_identify_total_failure_falls_back_to_manual_form(hass: HomeAssistant):
    """Reachable host but both identify fields missing → manual fallback with host only."""
    flow_id = await _start_local_flow(hass, [])
    await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "manual_ip"})

    with (
        patch("custom_components.sungrow.helpers.async_test_modbus_host", return_value=True),
        patch(
            "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity",
            return_value=(None, None),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.5"})
    assert result["step_id"] == "local_setup"
    defaults = {marker.schema: marker.default() for marker in result["data_schema"].schema}
    assert defaults[CONF_MODBUS_HOST] == "10.0.0.5"


# ---------------------------------------------------------------------------
# Confirm step + comms probe
# ---------------------------------------------------------------------------


async def test_confirm_probe_failure_shows_error_and_holds_step(hass: HomeAssistant):
    """A failed comms probe on confirm submit surfaces comms_probe_failed and stays on the confirm form."""
    dongle = WinetDongle(host="192.168.1.10", serial="SN-PROBE", model="SG5.0RS", mdns_name=None)
    flow_id = await _start_local_flow(hass, [dongle])
    await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "192.168.1.10"})

    # Second identity read returns nothing → comms probe failed.
    with patch(
        "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity",
        return_value=(None, None),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={})
    assert result["step_id"] == "local_confirm_identified"
    assert result["errors"] == {"base": "comms_probe_failed"}


async def test_confirm_serial_mismatch_refuses_to_create(hass: HomeAssistant):
    """A serial that changes between discovery and probe → serial_mismatch error, no entry."""
    dongle = WinetDongle(host="192.168.1.10", serial="SN-ORIGINAL", model="SG5.0RS", mdns_name=None)
    flow_id = await _start_local_flow(hass, [dongle])
    await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "192.168.1.10"})

    with patch(
        "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity",
        return_value=("SG5.0RS", "SN-DIFFERENT"),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={})
    assert result["step_id"] == "local_confirm_identified"
    assert result["errors"] == {"base": "serial_mismatch"}
