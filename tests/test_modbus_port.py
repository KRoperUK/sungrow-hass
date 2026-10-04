"""Custom Modbus TCP port for local entries — e.g. behind an evcc modbus proxy (#485).

Covers every place a local endpoint is chosen (manual wizard, fallback form, import,
reconfigure, options), that the reachability probe and identify read are aimed at the
chosen port, that the coordinator resolves options → data → 502, and that WiNet-S
discovery keeps 502 and never re-points a proxied entry at the dongle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow._config_flow._helpers import WinetDongle, async_read_modbus_identity
from custom_components.sungrow.config_flow import SungrowConfigFlow
from custom_components.sungrow.const import (
    CONF_DISCOVERY_MANAGED_HOST,
    CONF_MODBUS_DEBUG_DAILY_YIELD,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODEL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TRANSPORT,
    DOMAIN,
    TRANSPORT_CLOUD_ONLY,
    TRANSPORT_MODBUS_ONLY,
)
from custom_components.sungrow.coordinator import SungrowPlantCoordinator
from custom_components.sungrow.helpers import async_test_modbus_host, resolve_modbus_port
from custom_components.sungrow.migration import _async_split_legacy_hybrid

from .test_config_flow import _winet_discovery

_PROBE = "custom_components.sungrow.helpers.async_test_modbus_host"
_IDENTIFY = "custom_components.sungrow._config_flow.modbus_only.async_read_modbus_identity"
_DISCOVER = "custom_components.sungrow._config_flow.modbus_only.async_discover_winet_dongles"


@pytest.fixture(autouse=True)
def mock_client_session():
    """Mock async_get_clientsession so no real ClientSession is created."""
    with patch(
        "custom_components.sungrow._config_flow._base.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


def _local_entry(hass: HomeAssistant, *, data: dict[str, Any] | None = None, options: dict[str, Any] | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "SN485",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "10.0.0.9",
            **(data or {}),
        },
        options={CONF_SCAN_INTERVAL: 30, **(options or {})},
        unique_id="modbus_SN485",
        version=SungrowConfigFlow.VERSION,
    )
    entry.add_to_hass(hass)
    return entry


async def _manual_ip_flow(hass: HomeAssistant, dongles: list[WinetDongle] | None = None) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(_DISCOVER, return_value=dongles or []):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY}
        )
    step = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={"choice": "manual_ip"})
    assert step["step_id"] == "local_manual_ip"
    return result["flow_id"]


def _schema_defaults(result: Any) -> dict[str, Any]:
    return {str(marker.schema): marker.default() for marker in result["data_schema"].schema}


# ---------------------------------------------------------------------------
# Resolution helper + probe primitives
# ---------------------------------------------------------------------------


def test_resolve_modbus_port_order():
    """Options override data, data overrides the 502 default."""
    assert resolve_modbus_port({}, {}) == 502
    assert resolve_modbus_port({}, {CONF_MODBUS_PORT: 5020}) == 5020
    assert resolve_modbus_port({CONF_MODBUS_PORT: 1502}, {CONF_MODBUS_PORT: 5020}) == 1502
    # Stored as a string by an older/hand-edited entry still resolves.
    assert resolve_modbus_port({}, {CONF_MODBUS_PORT: "5020"}) == 5020
    # Blank/None means "not set", never port 0.
    assert resolve_modbus_port({CONF_MODBUS_PORT: None}, {CONF_MODBUS_PORT: ""}) == 502


async def test_reachability_probe_uses_given_port():
    """The TCP probe opens the connection on the requested port, not a hard-coded 502."""
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    with patch("asyncio.open_connection", AsyncMock(return_value=(MagicMock(), writer))) as conn:
        assert await async_test_modbus_host("10.0.0.2", 5020) is True
    conn.assert_awaited_once_with("10.0.0.2", 5020)


async def test_identity_read_uses_given_port():
    """The identify read builds its Modbus client against host:port."""
    client = MagicMock()
    client.async_read_realtime = AsyncMock(
        return_value={"inverter_serial": {"value": "SN485"}, "device_type_code": {"value": None}}
    )
    with patch("custom_components.sungrow.modbus.SungrowModbusClient", return_value=client) as cls:
        model, serial = await async_read_modbus_identity("10.0.0.2", 5020)
    cls.assert_called_once_with("10.0.0.2", port=5020)
    assert serial == "SN485"
    assert model is None
    client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_port", "options_port", "expected"),
    [
        (None, None, 502),  # pre-#485 entry: neither key stored
        (5020, None, 5020),  # set by the config flow (wizard / reconfigure)
        (5020, 1502, 1502),  # options override wins
        (None, 1502, 1502),
    ],
)
async def test_coordinator_resolves_port_options_then_data_then_default(
    hass: HomeAssistant, data_port, options_port, expected
):
    """The Modbus client is built on options → data → 502, so a proxy port is honoured."""
    data: dict[str, Any] = {CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY, CONF_MODBUS_HOST: "10.0.0.5"}
    options: dict[str, Any] = {}
    if data_port is not None:
        data[CONF_MODBUS_PORT] = data_port
    if options_port is not None:
        options[CONF_MODBUS_PORT] = options_port
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options)

    client = SungrowPlantCoordinator._build_modbus_client(entry)

    assert client is not None
    assert (client.host, client.port) == ("10.0.0.5", expected)


# ---------------------------------------------------------------------------
# Guided wizard: manual IP → confirm
# ---------------------------------------------------------------------------


async def test_manual_ip_custom_port_reaches_probe_identify_and_entry(hass: HomeAssistant):
    """A proxy port typed in the wizard is probed, identified on, confirmed on, and stored."""
    flow_id = await _manual_ip_flow(hass)

    with (
        patch(_PROBE, return_value=True) as probe,
        patch(_IDENTIFY, return_value=("SG3.6RS", "SN485")) as identify,
    ):
        confirm = await hass.config_entries.flow.async_configure(
            flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}
        )
    probe.assert_awaited_once_with("10.0.0.20", 5020)
    identify.assert_awaited_once_with("10.0.0.20", 5020)
    assert confirm["step_id"] == "local_confirm_identified"
    # The non-standard port is shown so the user can see what is being added.
    assert confirm["description_placeholders"]["host"] == "10.0.0.20:5020"

    with (
        patch(_IDENTIFY, return_value=("SG3.6RS", "SN485")) as reread,
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        created = await hass.config_entries.flow.async_configure(flow_id, user_input={})
        await hass.async_block_till_done()

    reread.assert_awaited_once_with("10.0.0.20", 5020)
    assert created["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_MODBUS_HOST] == "10.0.0.20"
    assert created["data"][CONF_MODBUS_PORT] == 5020
    assert created["result"].unique_id == "modbus_SN485"


async def test_manual_ip_defaults_to_502(hass: HomeAssistant):
    """Leaving the port untouched probes and stores the standard port."""
    flow_id = await _manual_ip_flow(hass)

    with patch(_PROBE, return_value=False) as probe:
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20"})
    probe.assert_awaited_once_with("10.0.0.20", 502)
    assert result["errors"] == {"base": "host_unreachable"}
    assert _schema_defaults(result)[CONF_MODBUS_PORT] == 502


async def test_manual_ip_unreachable_keeps_typed_port(hass: HomeAssistant):
    """A failed probe re-renders the form with the port the user typed, not 502."""
    flow_id = await _manual_ip_flow(hass)

    with patch(_PROBE, return_value=False):
        result = await hass.config_entries.flow.async_configure(
            flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}
        )
    assert result["step_id"] == "local_manual_ip"
    assert _schema_defaults(result) == {CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}


@pytest.mark.parametrize("bad_port", [0, 65536, -1, "not-a-port"])
async def test_manual_ip_rejects_out_of_range_port(hass: HomeAssistant, bad_port):
    """Ports outside 1–65535 (or non-numeric) are rejected by the form schema."""
    flow_id = await _manual_ip_flow(hass)

    with patch(_PROBE, return_value=True) as probe, pytest.raises(data_entry_flow.InvalidData):
        await hass.config_entries.flow.async_configure(
            flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: bad_port}
        )
    probe.assert_not_awaited()


async def test_discovered_dongle_uses_502_even_after_manual_port(hass: HomeAssistant):
    """Picking a discovered WiNet-S after typing a proxy port goes back to the dongle's 502."""
    dongle = WinetDongle(host="192.168.1.42", serial="SN485", model="SG3.6RS", mdns_name=None)
    flow_id = await _manual_ip_flow(hass, [dongle])
    with patch(_PROBE, return_value=False):
        await hass.config_entries.flow.async_configure(
            flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}
        )
    # Start over from the picker in a fresh flow sharing nothing but the dongle cache.
    flow_id = (await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER}))[
        "flow_id"
    ]
    with patch(_DISCOVER, return_value=[dongle]):
        await hass.config_entries.flow.async_configure(flow_id, user_input={CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY})
    confirm = await hass.config_entries.flow.async_configure(flow_id, user_input={"choice": "192.168.1.42"})
    assert confirm["description_placeholders"]["host"] == "192.168.1.42"

    with (
        patch(_IDENTIFY, return_value=("SG3.6RS", "SN485")) as reread,
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        created = await hass.config_entries.flow.async_configure(flow_id, user_input={})
        await hass.async_block_till_done()
    reread.assert_awaited_once_with("192.168.1.42", 502)
    assert created["data"][CONF_MODBUS_PORT] == 502


async def test_fallback_form_carries_and_stores_port(hass: HomeAssistant):
    """When identify misses, the manual-details form is pre-filled with the port and stores it."""
    flow_id = await _manual_ip_flow(hass)
    with patch(_PROBE, return_value=True), patch(_IDENTIFY, return_value=(None, None)):
        setup = await hass.config_entries.flow.async_configure(
            flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}
        )
    assert setup["step_id"] == "local_setup"
    assert _schema_defaults(setup)[CONF_MODBUS_PORT] == 5020

    with (
        patch(_PROBE, return_value=True) as probe,
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        created = await hass.config_entries.flow.async_configure(
            flow_id,
            user_input={
                CONF_MODBUS_HOST: "10.0.0.20",
                CONF_MODBUS_PORT: 5021,
                CONF_SERIAL: "SN485",
                CONF_MODEL: "SG3.6RS",
            },
        )
        await hass.async_block_till_done()
    probe.assert_awaited_once_with("10.0.0.20", 5021)
    assert created["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_MODBUS_PORT] == 5021


async def test_readding_same_inverter_via_proxy_updates_existing_entry(hass: HomeAssistant):
    """unique_id stays the serial: the same inverter via a proxy updates host+port, no duplicate."""
    entry = _local_entry(hass, data={CONF_MODBUS_PORT: 502, CONF_DISCOVERY_MANAGED_HOST: True})
    flow_id = await _manual_ip_flow(hass)
    with patch(_PROBE, return_value=True), patch(_IDENTIFY, return_value=("SG3.6RS", "SN485")):
        await hass.config_entries.flow.async_configure(
            flow_id, user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}
        )
    with (
        patch(_IDENTIFY, return_value=("SG3.6RS", "SN485")),
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, user_input={})
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_MODBUS_HOST] == "10.0.0.20"
    assert entry.data[CONF_MODBUS_PORT] == 5020
    assert entry.data[CONF_DISCOVERY_MANAGED_HOST] is False


# ---------------------------------------------------------------------------
# Import (legacy hybrid split)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "stored"), [(5020, 5020), ("5020", 5020), (None, 502), (70000, 502)])
async def test_import_stores_validated_port(hass: HomeAssistant, given, stored):
    """SOURCE_IMPORT keeps a valid legacy port and falls back to 502 for a missing/invalid one."""
    data: dict[str, Any] = {
        CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
        CONF_SERIAL: "SN485",
        CONF_MODEL: "SG3.6RS",
        CONF_MODBUS_HOST: "10.0.0.5",
        CONF_MODBUS_PORT: given,
    }
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=data
        )
        await hass.async_block_till_done()
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODBUS_PORT] == stored


async def test_legacy_hybrid_split_carries_port(hass: HomeAssistant):
    """A legacy cloud entry's ``modbus_port`` option is handed to the split-off local entry."""
    cloud = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TRANSPORT: TRANSPORT_CLOUD_ONLY},
        options={CONF_MODBUS_HOST: "10.0.0.5", CONF_MODBUS_PORT: 5020},
        unique_id="app",
    )
    cloud.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=cloud.entry_id, identifiers={(DOMAIN, "inv")}, serial_number="SN485", model="SG3.6RS"
    )

    with patch.object(hass.config_entries.flow, "async_init", AsyncMock()) as init:
        _async_split_legacy_hybrid(hass, cloud)
        await hass.async_block_till_done()

    assert CONF_MODBUS_PORT not in cloud.options
    init.assert_called_once()
    assert init.call_args.kwargs["data"][CONF_MODBUS_PORT] == 5020


# ---------------------------------------------------------------------------
# Reconfigure
# ---------------------------------------------------------------------------


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure_modbus"
    return result


async def test_reconfigure_defaults_to_resolved_port(hass: HomeAssistant):
    """The reconfigure form pre-fills the port the entry actually uses."""
    entry = _local_entry(hass, data={CONF_MODBUS_PORT: 5020}, options={CONF_MODBUS_PORT: 1502})
    result = await _reconfigure(hass, entry)
    assert _schema_defaults(result) == {CONF_MODBUS_HOST: "10.0.0.9", CONF_MODBUS_PORT: 1502}


async def test_reconfigure_changes_port_and_clears_option_override(hass: HomeAssistant):
    """Reconfigure probes the new endpoint, stores the port in data and drops a stale override."""
    entry = _local_entry(hass, data={CONF_DISCOVERY_MANAGED_HOST: True}, options={CONF_MODBUS_PORT: 1502})
    result = await _reconfigure(hass, entry)

    with (
        patch(_PROBE, return_value=True) as probe,
        patch("custom_components.sungrow.async_setup_entry", return_value=True),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "10.0.0.20", CONF_MODBUS_PORT: 5020}
        )
        await hass.async_block_till_done()

    probe.assert_awaited_once_with("10.0.0.20", 5020)
    assert done["reason"] == "reconfigure_successful"
    assert entry.data[CONF_MODBUS_HOST] == "10.0.0.20"
    assert entry.data[CONF_MODBUS_PORT] == 5020
    # A discovered entry pointed at a proxy is pinned, so rediscovery can't undo it.
    assert entry.data[CONF_DISCOVERY_MANAGED_HOST] is False
    assert CONF_MODBUS_PORT not in entry.options
    assert entry.options[CONF_SCAN_INTERVAL] == 30
    assert resolve_modbus_port(entry.options, entry.data) == 5020


async def test_reconfigure_unreachable_endpoint_is_not_saved(hass: HomeAssistant):
    """A wrong port fails the probe: the form returns with an error and the entry is untouched."""
    entry = _local_entry(hass, data={CONF_MODBUS_PORT: 502})
    result = await _reconfigure(hass, entry)

    with patch(_PROBE, return_value=False):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "10.0.0.9", CONF_MODBUS_PORT: 5020}
        )

    assert again["type"] == data_entry_flow.FlowResultType.FORM
    assert again["errors"] == {"base": "host_unreachable"}
    assert _schema_defaults(again)[CONF_MODBUS_PORT] == 5020
    assert entry.data[CONF_MODBUS_PORT] == 502


async def test_reconfigure_rejects_invalid_port(hass: HomeAssistant):
    """Reconfigure validates the port range too."""
    entry = _local_entry(hass)
    result = await _reconfigure(hass, entry)
    with patch(_PROBE, return_value=True) as probe, pytest.raises(data_entry_flow.InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_MODBUS_HOST: "10.0.0.9", CONF_MODBUS_PORT: 0}
        )
    probe.assert_not_awaited()


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


async def _options(hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any] | None = None):
    client = MagicMock()
    client.async_read_realtime = AsyncMock(return_value={"grid_frequency": {"value": 49.9}})
    with patch("custom_components.sungrow.modbus.SungrowModbusClient", return_value=client):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        if user_input is None:
            return result
        done = await hass.config_entries.options.async_configure(result["flow_id"], user_input=user_input)
        await hass.async_block_till_done()
        return done


async def test_options_shows_resolved_port(hass: HomeAssistant):
    """The options form pre-fills the port in use (data here, since no override exists)."""
    entry = _local_entry(hass, data={CONF_MODBUS_PORT: 5020})
    result = await _options(hass, entry)
    assert result["step_id"] == "modbus_options"
    assert _schema_defaults(result)[CONF_MODBUS_PORT] == 5020


async def test_options_stores_port_override(hass: HomeAssistant):
    """A port different from entry data is stored as an options override and used on reload."""
    entry = _local_entry(hass)
    done = await _options(
        hass, entry, {CONF_SCAN_INTERVAL: 30, CONF_MODBUS_PORT: 5020, CONF_MODBUS_DEBUG_DAILY_YIELD: False}
    )
    assert done["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MODBUS_PORT] == 5020
    assert resolve_modbus_port(entry.options, entry.data) == 5020


async def test_options_same_port_as_data_stores_no_override(hass: HomeAssistant):
    """Submitting the data port unchanged stores no override, so Reconfigure stays authoritative."""
    entry = _local_entry(hass, data={CONF_MODBUS_PORT: 5020}, options={CONF_MODBUS_PORT: 1502})
    await _options(hass, entry, {CONF_SCAN_INTERVAL: 30, CONF_MODBUS_PORT: 5020})
    assert CONF_MODBUS_PORT not in entry.options
    assert resolve_modbus_port(entry.options, entry.data) == 5020


async def test_options_rejects_invalid_port(hass: HomeAssistant):
    """The options form validates the port range."""
    entry = _local_entry(hass)
    result = await _options(hass, entry)
    with pytest.raises(data_entry_flow.InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_SCAN_INTERVAL: 30, CONF_MODBUS_PORT: 65536}
        )


# ---------------------------------------------------------------------------
# Zeroconf
# ---------------------------------------------------------------------------


async def test_zeroconf_entry_stores_default_port(hass: HomeAssistant):
    """A discovered WiNet-S is set up on port 502."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_winet_discovery()
    )
    with patch("custom_components.sungrow.async_setup_entry", return_value=True):
        created = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
        await hass.async_block_till_done()
    assert created["data"][CONF_MODBUS_PORT] == 502


async def test_zeroconf_does_not_repoint_proxied_entry(hass: HomeAssistant):
    """A discovery-managed entry sent to a proxy port via options keeps its host on rediscovery."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TRANSPORT: TRANSPORT_MODBUS_ONLY,
            CONF_SERIAL: "A2340512345",
            CONF_MODEL: "SG3.6RS",
            CONF_MODBUS_HOST: "192.168.1.50",
            CONF_MODBUS_PORT: 502,
            CONF_DISCOVERY_MANAGED_HOST: True,
        },
        options={CONF_MODBUS_PORT: 5020},
        unique_id="modbus_A2340512345",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_winet_discovery(host="192.168.1.99")
    )
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_MODBUS_HOST] == "192.168.1.50"
