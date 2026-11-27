#!/usr/bin/env python3
"""Generate ``scripts/wallbox_snapshot_ranges.json`` from the mapped registers.

The AC22E-01 snapshot sweep (``wallbox_register_snapshot.py``) must run on
reporter machines with only ``pymodbus`` installed — no Home Assistant, and no
Python 3.14-only syntax (the integration floor). So instead of importing the
register maps at sweep time, this generator (run here, in the dev environment)
resolves each map family's sweep span once and stores it as plain data.
``tests/test_wallbox_snapshot.py`` fails when the committed JSON is out of
date — regenerate with::

    python scripts/generate_wallbox_snapshot_ranges.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.sungrow.modbus_registers import (  # noqa: E402
    HOLDING_CONTROL_MAPS,
    REGISTER_MAPS,
    SG_RS_HOLDING_PROBE_POINTS,
)

OUTPUT = REPO_ROOT / "scripts" / "wallbox_snapshot_ranges.json"


def _holding_span(family: str) -> list[int] | None:
    """Inclusive ``[start, end]`` of the mapped holding points, if any."""
    points = list(HOLDING_CONTROL_MAPS.get(family, ()))
    if not points and family.startswith("sg"):
        points = list(SG_RS_HOLDING_PROBE_POINTS)
    addresses = sorted(p.wire_address for p in points)
    return [addresses[0], addresses[-1]] if addresses else None


def main(output: Path = OUTPUT) -> None:
    """Write the per-family input/holding sweep spans as JSON."""
    families = {}
    for family, points in REGISTER_MAPS.items():
        addresses = sorted(p.address for p in points)
        families[family] = {
            "input": [addresses[0], addresses[-1]],
            "holding": _holding_span(family),
        }
    output.write_text(json.dumps({"families": families}, indent=2) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else OUTPUT)
