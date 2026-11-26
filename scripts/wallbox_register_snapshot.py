#!/usr/bin/env python3
"""Read-only Modbus snapshot sweep to identify wallbox registers (#578).

The AC22E-01 exposes no Modbus TCP endpoint of its own (reporter-observed: its
IP answers ping but refuses TCP/502), so the working hypothesis is that wallbox
state surfaces through additional registers on the RS485-linked inverter. This
script captures the raw ground truth needed to test that hypothesis: it sweeps
the inverter's input and holding registers in small read-only blocks and writes
one JSON snapshot per wallbox state (idle / car connected / charging). Diffing
the snapshots shows which addresses correlate with the wallbox.

Only ``read_input_registers`` / ``read_holding_registers`` are ever called —
there is deliberately no write path. Sweep ranges default to the span of the
integration's own mapped points for ``--family`` (no new register semantics are
claimed); anything beyond that comes from explicit ``--range`` windows. The
output records no host address so it is safe to paste into the issue as-is.

Usage (from the repository root)::

    python scripts/wallbox_register_snapshot.py --host 192.168.1.50 \\
        --family sh_rt --state idle -o snapshot_idle.json
    # ... change the wallbox state, then rerun with --state charging ...
    python scripts/wallbox_register_snapshot.py --host 192.168.1.50 \\
        --family sh_rt --state charging -o snapshot_charging.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pymodbus.client import ModbusTcpClient  # noqa: E402

from custom_components.sungrow.modbus_registers import (  # noqa: E402
    DEFAULT_MAX_BLOCK_SIZE,
    HOLDING_CONTROL_MAPS,
    MODBUS_MAX_READ_COUNT,
    REGISTER_MAPS,
    SG_RS_HOLDING_PROBE_POINTS,
)

ISSUE = 578
DEFAULT_PORT = 502
DEFAULT_TIMEOUT = 5


def parse_range(text: str) -> tuple[str, int, int]:
    """Parse a ``TYPE:START:COUNT`` window (``input:4999:100``)."""
    try:
        register_type, start, count = text.split(":")
        parsed = (register_type.strip().lower(), int(start), int(count))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected TYPE:START:COUNT, got {text!r}") from None
    if parsed[0] not in ("input", "holding"):
        raise argparse.ArgumentTypeError(f"register type must be input|holding, got {parsed[0]!r}")
    if not 0 <= parsed[1] <= 0xFFFF or parsed[2] < 1:
        raise argparse.ArgumentTypeError(f"bad window {text!r}")
    if parsed[1] + parsed[2] - 1 > 0xFFFF:
        raise argparse.ArgumentTypeError(f"window {text!r} runs past 0xFFFF")
    return parsed


def _holding_points(family: str) -> list[int]:
    """Wire addresses of the mapped holding points for ``family``."""
    points = list(HOLDING_CONTROL_MAPS.get(family, ()))
    if not points and family.startswith("sg"):
        points = list(SG_RS_HOLDING_PROBE_POINTS)
    return sorted(p.wire_address for p in points)


def _chunk_span(start: int, end: int, chunk_size: int) -> list[tuple[int, int]]:
    """Split an inclusive ``start..end`` span into ``(start, count)`` reads."""
    chunks = []
    cursor = start
    while cursor <= end:
        count = min(chunk_size, end - cursor + 1)
        chunks.append((cursor, count))
        cursor += count
    return chunks


def build_spans(
    family: str,
    chunk_size: int,
    extra: list[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    """Return the ordered ``(type, start, count)`` sweep for ``family``.

    Ranges cover the span of the integration's own mapped points (which the
    dongle is known to answer) so unknown registers inside the span are swept
    too; per-chunk failures are recorded, never fatal. Caller-supplied
    ``extra`` windows come last.
    """
    spans: list[tuple[str, int, int]] = []
    addresses = sorted(p.address for p in REGISTER_MAPS[family])
    for start, count in _chunk_span(addresses[0], addresses[-1], chunk_size):
        spans.append(("input", start, count))
    holding = _holding_points(family)
    if holding:
        for start, count in _chunk_span(holding[0], holding[-1], chunk_size):
            spans.append(("holding", start, count))
    for register_type, start, count in extra:
        for chunk_start, chunk_count in _chunk_span(start, start + count - 1, chunk_size):
            spans.append((register_type, chunk_start, chunk_count))
    return spans


def sweep(
    client: ModbusTcpClient,
    spans: list[tuple[str, int, int]],
    units: list[int],
) -> tuple[list[dict], int, int]:
    """Read every span for every unit; return ``(captures, ok, failed)``."""
    captures: list[dict] = []
    ok = failed = 0
    for unit in units:
        for register_type, start, count in spans:
            read = client.read_input_registers if register_type == "input" else client.read_holding_registers
            try:
                response = read(start, count=count, device_id=unit)
                values = list(response.registers)
            except Exception as err:  # noqa: BLE001 — best-effort probe
                captures.append(
                    {
                        "register_type": register_type,
                        "unit": unit,
                        "start": start,
                        "count": count,
                        "error": str(err),
                    }
                )
                failed += 1
                continue
            if response.isError() or len(values) != count:
                captures.append(
                    {
                        "register_type": register_type,
                        "unit": unit,
                        "start": start,
                        "count": count,
                        "error": f"short/error reply: {response!r}"[:200],
                    }
                )
                failed += 1
                continue
            captures.append(
                {
                    "register_type": register_type,
                    "unit": unit,
                    "start": start,
                    "count": count,
                    "values": values,
                }
            )
            ok += 1
    return captures, ok, failed


def build_parser() -> argparse.ArgumentParser:
    """Define the snapshot CLI."""
    parser = argparse.ArgumentParser(
        description="Read-only Modbus snapshot sweep for AC22E-01 register discovery (issue #578). Never writes.",
    )
    parser.add_argument("--host", required=True, help="Inverter Modbus TCP host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--family",
        default="sh_rt",
        choices=sorted(REGISTER_MAPS),
        help="Integration map family whose span seeds the sweep",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="Wallbox state label for this snapshot (e.g. idle, charging)",
    )
    parser.add_argument(
        "--type",
        default="both",
        choices=("input", "holding", "both"),
        help="Which register tables to sweep",
    )
    parser.add_argument(
        "--units",
        default="1",
        help="Comma-separated Modbus unit IDs to probe (default: 1)",
    )
    parser.add_argument(
        "--range",
        dest="extra",
        action="append",
        default=[],
        type=parse_range,
        metavar="TYPE:START:COUNT",
        help="Extra window to sweep (repeatable), e.g. --range input:6000:100",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_MAX_BLOCK_SIZE,
        help=f"Registers per read, max {MODBUS_MAX_READ_COUNT}",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Connect timeout (s)")
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Snapshot JSON path (default: stdout)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Connect, sweep, and emit the snapshot JSON."""
    args = build_parser().parse_args(argv)
    chunk_size = min(args.chunk_size, MODBUS_MAX_READ_COUNT)
    try:
        units = [int(u) for u in args.units.split(",") if u.strip() != ""]
    except ValueError:
        print(f"bad --units value: {args.units!r}", file=sys.stderr)
        return 2
    if not units or any(not 0 <= u <= 247 for u in units):
        print(f"bad --units value: {args.units!r}", file=sys.stderr)
        return 2

    spans = [
        span for span in build_spans(args.family, chunk_size, args.extra) if args.type == "both" or span[0] == args.type
    ]

    client = ModbusTcpClient(args.host, port=args.port, timeout=args.timeout)
    if not client.connect():
        print(f"cannot connect to {args.host}:{args.port}", file=sys.stderr)
        return 1
    try:
        captures, ok, failed = sweep(client, spans, units)
    finally:
        client.close()

    snapshot = {
        "tool": "scripts/wallbox_register_snapshot.py",
        "issue": ISSUE,
        "family": args.family,
        "state": args.state,
        "units": units,
        "chunk_size": chunk_size,
        "reads_ok": ok,
        "reads_failed": failed,
        # No host recorded: the file is meant to be pasted into the issue.
        "captures": captures,
    }
    text = json.dumps(snapshot, indent=2)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(
        f"state={args.state} spans={len(spans)} units={units} ok={ok} failed={failed}",
        file=sys.stderr,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
