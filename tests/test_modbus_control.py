"""Unit tests for ModbusControl (#220 Phase 1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from custom_components.sungrow.modbus_control import ModbusControl, ModbusControlError


def _client(*, family: str = "sg_rs") -> MagicMock:
    client = MagicMock()
    client.model = family
    client.async_read_holding = AsyncMock(return_value=[1000])
    client.async_write_holding = AsyncMock()
    return client


async def test_supported_parameters_for_sg_rs():
    control = ModbusControl(_client(), family="sg_rs")
    assert "active_power_limit_ratio" in control.supported_parameters
    assert "limited_power_switch" in control.supported_parameters
    assert "energy_management_mode" not in control.supported_parameters


async def test_check_update_support_reads_ratio():
    client = _client()
    control = ModbusControl(client, family="sg_rs")
    assert await control.async_check_update_support("sn_inv") is True
    client.async_read_holding.assert_awaited()


async def test_check_update_support_false_on_read_error():
    from custom_components.sungrow.modbus import SungrowModbusError

    client = _client()
    client.async_read_holding = AsyncMock(side_effect=SungrowModbusError("exception_code=2"))
    control = ModbusControl(client, family="sg_rs")
    assert await control.async_check_update_support("sn_inv") is False


async def test_update_writes_and_verifies():
    client = _client()
    client.async_read_holding = AsyncMock(return_value=[900])
    control = ModbusControl(client, family="sg_rs")
    out = await control.async_update_parameters("sn_inv", {"active_power_limit_ratio": "900"})
    client.async_write_holding.assert_awaited_once_with(5007, 900)
    assert out[0]["code"] == "active_power_limit_ratio"
    assert out[0]["value"] == "900"


async def test_update_rejects_unknown_param():
    control = ModbusControl(_client(), family="sg_rs")
    with pytest.raises(ModbusControlError, match="not available"):
        await control.async_update_parameters("sn_inv", {"energy_management_mode": "2"})


async def test_update_rejects_readback_mismatch():
    client = _client()
    client.async_write_holding = AsyncMock()
    client.async_read_holding = AsyncMock(return_value=[1])  # not what we wrote
    control = ModbusControl(client, family="sg_rs")
    with pytest.raises(ModbusControlError, match="read-back mismatch"):
        await control.async_update_parameters("sn_inv", {"active_power_limit_ratio": "900"})


async def test_read_parameters_shape():
    client = _client()
    client.async_read_holding = AsyncMock(return_value=[170])
    control = ModbusControl(client, family="sg_rs")
    rows = await control.async_read_parameters("sn_inv", ["limited_power_switch"])
    assert rows[0]["id"] == "10007"
    assert rows[0]["code"] == "limited_power_switch"
    assert rows[0]["value"] == "170"


async def test_empty_family_has_no_support():
    client = _client(family="unknown_family")
    control = ModbusControl(client, family="unknown_family")
    assert control.supported_parameters == frozenset()
    assert await control.async_check_update_support("x") is False


# ---------------------------------------------------------------------------
# SH hybrid holding maps (#331)
# ---------------------------------------------------------------------------
# The SH hybrid families expose battery/EMS/export controls through the
# 13049..13086 holding block. SH-RT and SH-RS diverge on the active-power-limit
# addresses (13088/13089 vs 31203/31204 respectively) per TCzerny's field notes.


_SH_HYBRID_PARAMS = {
    "energy_management_mode",
    "charge_discharge_command",
    "charge_discharge_power",
    "soc_upper_limit",
    "soc_lower_limit",
    "feed_in_limitation_value",
    "feed_in_limitation",
    "limited_power_switch",
    "active_power_limit_ratio",
}


@pytest.mark.parametrize("family", ["sh_rt", "sh_rs"])
async def test_sh_hybrid_supported_parameters_cover_ems_battery_export(family: str):
    """Both SH hybrid sub-families expose the full battery/EMS/export control set."""
    control = ModbusControl(_client(family=family), family=family)
    assert control.supported_parameters >= _SH_HYBRID_PARAMS
    # The SG-only string-inverter probe points aren't in the SH maps.
    assert "active_power_limit_ratio" in control.supported_parameters


async def test_sh_rt_writes_ems_and_charge_discharge_to_hybrid_block():
    """`energy_management_mode` and forced-charge/discharge hit the 13049-13051 wires."""
    client = _client(family="sh_rt")
    client.async_read_holding = AsyncMock(side_effect=[[2], [170], [3000]])
    control = ModbusControl(client, family="sh_rt")
    await control.async_update_parameters(
        "sn_inv",
        {"energy_management_mode": "2", "charge_discharge_command": "170", "charge_discharge_power": "3000"},
    )
    assert client.async_write_holding.await_args_list == [
        call(13049, 2),
        call(13050, 170),
        call(13051, 3000),
    ]


async def test_sh_rt_active_power_limit_hits_13088_13089():
    """SH-RT uses the 13088/13089 active-power-limit block (per mkaiser/TCzerny)."""
    client = _client(family="sh_rt")
    client.async_read_holding = AsyncMock(side_effect=[[170], [1000]])
    control = ModbusControl(client, family="sh_rt")
    await control.async_update_parameters(
        "sn_inv",
        {"limited_power_switch": "170", "active_power_limit_ratio": "1000"},
    )
    assert client.async_write_holding.await_args_list == [
        call(13088, 170),
        call(13089, 1000),
    ]


async def test_sh_rs_active_power_limit_hits_31203_31204_split():
    """SH-RS uses the 31203/31204 split — TCzerny confirmed 13088/13089 non-functional there."""
    client = _client(family="sh_rs")
    client.async_read_holding = AsyncMock(side_effect=[[170], [1000]])
    control = ModbusControl(client, family="sh_rs")
    await control.async_update_parameters(
        "sn_inv",
        {"limited_power_switch": "170", "active_power_limit_ratio": "1000"},
    )
    assert client.async_write_holding.await_args_list == [
        call(31203, 170),
        call(31204, 1000),
    ]


async def test_sh_hybrid_shares_common_soc_and_export_wires():
    """SoC and export-limit registers are the same for SH-RT and SH-RS."""
    for family in ("sh_rt", "sh_rs"):
        client = _client(family=family)
        client.async_read_holding = AsyncMock(side_effect=[[900], [200]])
        control = ModbusControl(client, family=family)
        await control.async_update_parameters(
            "sn_inv",
            {"soc_upper_limit": "900", "soc_lower_limit": "200"},
        )
        assert client.async_write_holding.await_args_list == [
            call(13057, 900),
            call(13058, 200),
        ]


async def test_denylist_still_blocks_start_stop_on_sh_hybrid():
    """The dispatch path must never target start/stop, even on hybrids that map it read-side."""
    from custom_components.sungrow.modbus_registers import (
        HOLDING_CONTROL_MAPS,
        HOLDING_WRITE_DENYLIST_WIRE,
        HoldingControlPoint,
    )

    # Simulate a rogue map entry pointing at 5005 (start/stop) on sh_rt. This should
    # be caught by _resolve() before any wire access.
    poison_point = HoldingControlPoint(
        param="power_on",  # not a canonical cloud param, but that's OK — _resolve
        # keys on the param dict which we're monkey-patching.
        wire_address=5005,
        description="rogue mapping",
        param_code=None,
    )
    control = ModbusControl(_client(family="sh_rt"), family="sh_rt")
    # Force-insert the poison point so we can prove the guardrail bites.
    object.__setattr__(control, "_by_param", {**control._by_param, "power_on": poison_point})  # noqa: SLF001

    assert 5005 in HOLDING_WRITE_DENYLIST_WIRE  # sanity: still on the denylist
    assert HOLDING_CONTROL_MAPS["sh_rt"], "sh_rt should map some params"
    with pytest.raises(ModbusControlError, match="denylisted"):
        await control.async_update_parameters("sn_inv", {"power_on": "0xCE"})
