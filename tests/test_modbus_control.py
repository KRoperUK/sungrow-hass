"""Unit tests for ModbusControl (#220 Phase 1)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
