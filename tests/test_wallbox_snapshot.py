"""Tests for the read-only wallbox snapshot sweep (#578)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SPEC = importlib.util.spec_from_file_location("wallbox_register_snapshot", SCRIPTS / "wallbox_register_snapshot.py")
assert SPEC is not None and SPEC.loader is not None
snapshot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = snapshot
SPEC.loader.exec_module(snapshot)


class _Reply:
    """Minimal pymodbus-like read response."""

    def __init__(self, values=None, error=False):
        self.registers = values
        self._error = error

    def isError(self):  # noqa: N802 — pymodbus API spelling
        return self._error


class _Client:
    """Stub ModbusTcpClient: answers unit 1, errors elsewhere."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def connect(self):
        return True

    def close(self):
        pass

    def _read(self, address, count, device_id):
        self.calls.append((address, count, device_id))
        if device_id != 1:
            raise ConnectionError("no such unit")
        return _Reply(values=[address + i for i in range(count)])

    def read_input_registers(self, address, count=1, device_id=1, **kwargs):
        return self._read(address, count, device_id)

    def read_holding_registers(self, address, count=1, device_id=1, **kwargs):
        return self._read(address, count, device_id)


def _patch_client(monkeypatch):
    monkeypatch.setattr(snapshot, "ModbusTcpClient", _Client)


def test_spans_cover_mapped_span_in_capped_chunks():
    """Sweep stays within one Modbus read per chunk and covers the span."""
    spans = snapshot.build_spans("sh_rt", 100, [])
    assert spans
    assert all(count <= 100 for _, _, count in spans)
    inputs = [(s, c) for t, s, c in spans if t == "input"]
    assert inputs[0][0] == 4951
    assert inputs[-1][0] + inputs[-1][1] - 1 == 13279
    assert any(t == "holding" for t, _, _ in spans)


def test_sweep_records_values_and_unit_errors(monkeypatch):
    """Good reads record values; a dead unit records an error, not a crash."""
    _patch_client(monkeypatch)
    client = _Client()
    spans = [("input", 4999, 2), ("holding", 13049, 1)]
    captures, ok, failed = snapshot.sweep(client, spans, [1, 2])
    assert ok == 2
    assert failed == 2
    assert captures[0]["values"] == [4999, 5000]
    assert "error" in captures[-1]


def test_sweep_records_error_replies(monkeypatch):
    """A protocol-level error reply is data, not an exception."""

    class _ErrClient(_Client):
        def read_input_registers(self, address, count=1, device_id=1, **kwargs):
            return _Reply(error=True)

    monkeypatch.setattr(snapshot, "ModbusTcpClient", _ErrClient)
    captures, ok, failed = snapshot.sweep(_ErrClient(), [("input", 4999, 1)], [1])
    assert (ok, failed) == (0, 1)
    assert "error" in captures[0]


def test_main_writes_paste_safe_snapshot(monkeypatch, tmp_path, capsys):
    """End to end: JSON carries state+units, never the host address."""
    _patch_client(monkeypatch)
    out = tmp_path / "snap.json"
    rc = snapshot.main(
        [
            "--host",
            "192.0.2.10",
            "--family",
            "sg_rs",
            "--state",
            "idle",
            "--type",
            "input",
            "-o",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["state"] == "idle"
    assert payload["units"] == [1]
    assert payload["reads_failed"] == 0
    assert "192.0.2.10" not in out.read_text(encoding="utf-8")
    assert capsys.readouterr().out == ""  # JSON went to the file


def test_main_fails_closed_when_nothing_answers(monkeypatch):
    """All reads failing is exit 1, not a silent empty snapshot."""
    _patch_client(monkeypatch)
    rc = snapshot.main(["--host", "h", "--units", "2", "--state", "idle", "--type", "holding"])
    assert rc == 1


@pytest.mark.parametrize("units", ["x", "300", ""])
def test_main_rejects_bad_units(monkeypatch, units):
    """Unit IDs outside 0..247 never reach the wire."""
    _patch_client(monkeypatch)
    rc = snapshot.main(["--host", "h", "--units", units, "--state", "idle"])
    assert rc == 2
