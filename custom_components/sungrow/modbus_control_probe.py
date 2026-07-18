"""Live probe helpers for local Modbus holding-register control (#220 spike).

Read-only by default. Writes require ``SUNGROW_MODBUS_WRITE_OK=1`` and only target
explicit write candidates (never the denylist). Results classify as supported /
read_only / unsupported / inconclusive so #220 can go/no-go before HA wiring.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .modbus import SungrowModbusError
from .modbus_registers import (
    HOLDING_WRITE_DENYLIST_WIRE,
    SG_RS_HOLDING_PROBE_POINTS,
    HoldingProbePoint,
)

_LOGGER = logging.getLogger(__name__)

WRITE_OK_ENV = "SUNGROW_MODBUS_WRITE_OK"


class HoldingClient(Protocol):
    """Minimal client surface used by the probe (production or mock)."""

    async def async_read_holding(self, address: int, count: int = 1) -> list[int]: ...

    async def async_write_holding(self, address: int, value: int) -> None: ...


@dataclass(frozen=True)
class HoldingProbeResult:
    """One probe observation for a holding register."""

    name: str
    wire_address: int
    kind: str  # "read" | "write_noop" | "write_denied"
    ok: bool
    raw: int | None
    detail: str


def writes_allowed() -> bool:
    """True when the environment explicitly permits a live holding write."""
    return os.environ.get(WRITE_OK_ENV, "").strip() == "1"


def classify_holding_probe_results(results: list[HoldingProbeResult]) -> str:
    """Classify spike outcome for #220 go/no-go.

    - ``supported``: at least one write candidate was written and read back
    - ``read_only``: write candidates readable but writes skipped or failed
    - ``unsupported``: no probe point readable
    - ``inconclusive``: empty results or only connection-level failures
    """
    if not results:
        return "inconclusive"

    reads = [r for r in results if r.kind == "read"]
    writes = [r for r in results if r.kind == "write_noop"]
    ok_reads = [r for r in reads if r.ok]
    ok_writes = [r for r in writes if r.ok]

    if ok_writes:
        return "supported"
    if ok_reads:
        # Readable but no successful write (gated off or write failed).
        return "read_only"
    # All failed — distinguish total connection death from illegal addresses.
    if all("connect" in r.detail.lower() or "not connected" in r.detail.lower() for r in results):
        return "inconclusive"
    if any(r.ok for r in results):
        return "read_only"
    return "unsupported"


def _is_denied(address: int) -> bool:
    return address in HOLDING_WRITE_DENYLIST_WIRE


async def probe_holding_points(
    client: HoldingClient,
    points: tuple[HoldingProbePoint, ...] | None = None,
    *,
    allow_write: bool | None = None,
) -> list[HoldingProbeResult]:
    """Read candidate holdings; optionally no-op rewrite write candidates.

    When ``allow_write`` is None, defers to :func:`writes_allowed`. Writes always
    re-apply the **current** raw value (no-op) and refuse denylisted addresses.
    """
    targets = points if points is not None else SG_RS_HOLDING_PROBE_POINTS
    do_write = writes_allowed() if allow_write is None else allow_write
    out: list[HoldingProbeResult] = []

    for point in targets:
        try:
            regs = await client.async_read_holding(point.wire_address, 1)
            raw = int(regs[0])
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="read",
                    ok=True,
                    raw=raw,
                    detail=f"raw={raw}",
                )
            )
        except SungrowModbusError as err:
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="read",
                    ok=False,
                    raw=None,
                    detail=str(err),
                )
            )
            continue
        except Exception as err:  # pylint: disable=broad-except
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="read",
                    ok=False,
                    raw=None,
                    detail=f"exception={type(err).__name__}: {err}",
                )
            )
            continue

        if not point.write_candidate:
            continue
        if not do_write:
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="write_denied",
                    ok=False,
                    raw=raw,
                    detail=f"write skipped ({WRITE_OK_ENV}!=1)",
                )
            )
            continue
        if _is_denied(point.wire_address):
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="write_denied",
                    ok=False,
                    raw=raw,
                    detail="address on HOLDING_WRITE_DENYLIST_WIRE",
                )
            )
            continue

        try:
            await client.async_write_holding(point.wire_address, raw)
            verify = await client.async_read_holding(point.wire_address, 1)
            vraw = int(verify[0])
            ok = vraw == raw
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="write_noop",
                    ok=ok,
                    raw=vraw,
                    detail=f"noop write raw={raw} readback={vraw}",
                )
            )
        except SungrowModbusError as err:
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="write_noop",
                    ok=False,
                    raw=raw,
                    detail=str(err),
                )
            )
        except Exception as err:  # pylint: disable=broad-except
            out.append(
                HoldingProbeResult(
                    name=point.name,
                    wire_address=point.wire_address,
                    kind="write_noop",
                    ok=False,
                    raw=raw,
                    detail=f"exception={type(err).__name__}: {err}",
                )
            )

    return out


def format_probe_summary(results: list[HoldingProbeResult], classification: str) -> dict[str, Any]:
    """JSON-serialisable summary for issue comments / diagnostics (no secrets)."""
    return {
        "classification": classification,
        "results": [
            {
                "name": r.name,
                "wire_address": r.wire_address,
                "kind": r.kind,
                "ok": r.ok,
                "raw": r.raw,
                "detail": r.detail,
            }
            for r in results
        ],
    }
