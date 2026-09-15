"""Validity tests for the shipped automation blueprints (#432).

The blueprints under ``blueprints/automation/sungrow/`` are documented in the
README and imported by users. These tests pin them so they cannot rot silently:
every file must parse, satisfy Home Assistant's automation-blueprint schema,
expose a device/entity selector scoped to this integration, and reference only
inputs it declares.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from annotatedyaml.input import extract_inputs
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.util.yaml import load_yaml_dict

BLUEPRINT_DIR = Path(__file__).parent.parent / "blueprints" / "automation" / "sungrow"

EXPECTED_BLUEPRINTS = {
    "cheap_tariff_charge.yaml",
    "pre_peak_top_up.yaml",
    "low_soc_or_fault_alert.yaml",
}


def _blueprint_files() -> list[Path]:
    return sorted(BLUEPRINT_DIR.glob("*.yaml"))


def test_expected_blueprints_present():
    """The three documented blueprints exist (and nothing shipped by accident)."""
    assert {p.name for p in _blueprint_files()} == EXPECTED_BLUEPRINTS


@pytest.mark.parametrize("path", _blueprint_files(), ids=lambda p: p.name)
def test_blueprint_is_valid(path: Path):
    """Each blueprint parses and satisfies the automation-blueprint schema."""
    data = load_yaml_dict(str(path))
    # Raises for a malformed blueprint block, a bad selector, a duplicate input, etc.
    blueprint = Blueprint(
        data,
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
        path=str(path),
    )
    assert blueprint.name
    assert blueprint.metadata.get("description")
    assert blueprint.inputs, "a blueprint must declare at least one input"


@pytest.mark.parametrize("path", _blueprint_files(), ids=lambda p: p.name)
def test_blueprint_references_only_declared_inputs(path: Path):
    """Every ``!input`` used in the file is a declared input (catches typos)."""
    data = load_yaml_dict(str(path))
    declared = set(data["blueprint"]["input"])
    referenced = extract_inputs(data)
    undeclared = referenced - declared
    assert not undeclared, f"{path.name} references undeclared inputs: {sorted(undeclared)}"


@pytest.mark.parametrize("path", _blueprint_files(), ids=lambda p: p.name)
def test_blueprint_selectors_are_scoped_to_the_integration(path: Path):
    """Device/entity selectors target this integration, not arbitrary entities."""
    data = load_yaml_dict(str(path))
    inputs = data["blueprint"]["input"]
    integration_scoped = 0
    for spec in inputs.values():
        selector = (spec or {}).get("selector") or {}
        for kind in ("device", "entity"):
            cfg = selector.get(kind)
            if isinstance(cfg, dict) and cfg.get("integration") == "sungrow":
                integration_scoped += 1
    assert integration_scoped, f"{path.name} has no sungrow-scoped device/entity selector"


def test_charge_blueprints_arm_dispatch_via_the_service():
    """The charging blueprints must arm dispatch through set_battery_mode (#112).

    Writing charge power alone never starts the EMS heartbeat; the command
    service owns it. Guard against a future edit that drops the service call and
    silently ships a blueprint that sets power but never actually dispatches.
    """
    for name in ("cheap_tariff_charge.yaml", "pre_peak_top_up.yaml"):
        text = (BLUEPRINT_DIR / name).read_text()
        assert "sungrow.set_battery_mode" in text, name
        assert "force_charge" in text, name
        assert "self_consumption" in text, name
