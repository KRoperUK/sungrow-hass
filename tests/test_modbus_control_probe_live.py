"""Live holding-register probe against a real WiNet-S (#220 spike).

Run (read-only)::

    SUNGROW_MODBUS_HOST=192.168.x.x pytest -m live tests/test_modbus_control_probe_live.py -v

Optional no-op write of the current active-power ratio (restores same value)::

    SUNGROW_MODBUS_HOST=192.168.x.x SUNGROW_MODBUS_WRITE_OK=1 \\
      pytest -m live tests/test_modbus_control_probe_live.py -v

Never set WRITE_OK unless you accept a single FC6 to the ratio register only.
"""

from __future__ import annotations

import json
import os

import pytest

from custom_components.sungrow.modbus import SungrowModbusClient
from custom_components.sungrow.modbus_control_probe import (
    classify_holding_probe_results,
    format_probe_summary,
    probe_holding_points,
)

pytestmark = pytest.mark.live


@pytest.fixture
def modbus_host() -> str:
    host = os.environ.get("SUNGROW_MODBUS_HOST", "").strip()
    if not host:
        pytest.skip("SUNGROW_MODBUS_HOST not set")
    return host


@pytest.fixture
def modbus_port() -> int:
    return int(os.environ.get("SUNGROW_MODBUS_PORT", "502"))


@pytest.fixture
def modbus_unit() -> int:
    return int(os.environ.get("SUNGROW_MODBUS_UNIT", "1"))


async def test_live_holding_probe_sg_rs(modbus_host: str, modbus_port: int, modbus_unit: int):
    """Probe SG-RS active-power holdings + hybrid EMS negative control on live hardware."""
    client = SungrowModbusClient(modbus_host, port=modbus_port, unit=modbus_unit, model="sg_rs")
    try:
        results = await probe_holding_points(client)
    finally:
        client.close()

    classification = classify_holding_probe_results(results)
    summary = format_probe_summary(results, classification)
    # Visible in pytest -s / CI logs for pasting onto #220.
    print("\n#220 holding probe summary:\n" + json.dumps(summary, indent=2))

    assert classification in {"supported", "read_only", "unsupported", "inconclusive"}
    # Soft expectation: at least attempt all default points.
    assert len(results) >= 3
