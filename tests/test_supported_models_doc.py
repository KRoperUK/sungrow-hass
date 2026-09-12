"""The generated model matrix must match :data:`MODEL_SPECS` (#437)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "generate_supported_models.py"
COMMITTED = REPO_ROOT / "docs" / "SUPPORTED_MODELS.md"


def test_supported_models_doc_is_up_to_date(tmp_path):
    """A stale compatibility table is worse than none, so CI fails when the page drifts.

    The page is regenerated through the real script — the same command the docs tell a
    contributor to run — rather than by re-rendering in-process, so this check also proves
    that the documented workflow reproduces the committed file.
    """
    output = tmp_path / "SUPPORTED_MODELS.md"
    subprocess.run([sys.executable, str(GENERATOR), str(output)], check=True, cwd=REPO_ROOT)

    assert output.read_text(encoding="utf-8") == COMMITTED.read_text(encoding="utf-8"), (
        "docs/SUPPORTED_MODELS.md is out of date — run: python scripts/generate_supported_models.py"
    )
