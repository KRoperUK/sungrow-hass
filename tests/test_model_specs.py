"""Tests for the per-model datasheet metadata catalog (#332)."""

from __future__ import annotations

import pytest

from custom_components.sungrow.model_specs import MODEL_SPECS, ModelSpec, spec_for


def test_spec_for_returns_none_on_empty_input():
    """Unknown / empty model codes fall back to None so callers can use their defaults."""
    assert spec_for(None) is None
    assert spec_for("") is None
    assert spec_for("   ") is None


def test_spec_for_handles_case_and_whitespace():
    """Model codes arriving from device metadata may be mixed case or whitespace-padded."""
    assert spec_for("sh10rt-20") is MODEL_SPECS["SH10RT-20"]
    assert spec_for("  SH10RT-20  ") is MODEL_SPECS["SH10RT-20"]
    assert spec_for("Sh3.6Rs") is MODEL_SPECS["SH3.6RS"]


def test_spec_for_returns_none_for_unmapped_model():
    """A model not in the catalog resolves to None (fallback path in callers)."""
    assert spec_for("SG250HX") is None  # commercial HX, deliberately not in the catalog
    assert spec_for("not-a-real-model") is None


@pytest.mark.parametrize(
    ("model", "expected_charge", "expected_discharge"),
    [
        # SH-RS single-phase hybrids share a 6.6 kW battery schedule; the SH10RS
        # jumps to 10.6 kW — this is the ceiling users on SH10RS need on the
        # charge/discharge slider (previously clamped to ~10 kW AC nameplate).
        ("SH3.0RS", 6600, 6600),
        ("SH5.0RS", 6600, 6600),
        ("SH10RS", 10600, 10600),
        # SH-RT variants track the same schedule across suffix generations
        # (base / -20 / -V112 / -V122) per the Sungrow family map.
        ("SH5.0RT", 7500, 6000),
        ("SH5.0RT-20", 7500, 6000),
        ("SH10RT", 10600, 10600),
        ("SH10RT-20", 10600, 10600),
    ],
)
def test_sh_hybrids_carry_battery_power_limits(model, expected_charge, expected_discharge):
    """SH hybrids must expose datasheet battery ceilings for the dispatch slider (#332)."""
    spec = spec_for(model)
    assert spec is not None, f"expected {model} in catalog"
    assert spec.max_charge_power == expected_charge
    assert spec.max_discharge_power == expected_discharge


@pytest.mark.parametrize("model", ["SG3.0RS", "SG10RS", "SG3.0RT", "SG20RT"])
def test_sg_string_inverters_have_no_battery_power(model):
    """PV-only string inverters carry no battery limits — callers must fall back."""
    spec = spec_for(model)
    assert spec is not None
    assert spec.max_charge_power is None
    assert spec.max_discharge_power is None


def test_spec_ac_rating_matches_model_kw():
    """Sanity: AC rating parses to the same kW as the model-code kW hint."""
    assert spec_for("SG3.6RS").max_ac_output_power == 3680  # SG*.6 = 3.68 kW special case
    assert spec_for("SG10RS").max_ac_output_power == 10000
    assert spec_for("SH10RT-20").max_ac_output_power == 10000


def test_every_spec_has_sensible_values():
    """Structural invariants: no zero/negative AC ratings; battery limits ≥ 0 when set."""
    for model, spec in MODEL_SPECS.items():
        assert isinstance(spec, ModelSpec), model
        assert spec.phases in (1, 3), f"{model}: phases must be 1 or 3, got {spec.phases}"
        assert spec.mppt_count >= 1, model
        assert spec.string_count >= 1, model
        assert spec.max_ac_output_power > 0, model
        assert spec.max_current > 0, model
        if spec.max_charge_power is not None:
            assert spec.max_charge_power > 0, model
        if spec.max_discharge_power is not None:
            assert spec.max_discharge_power > 0, model
        # Battery limits come as a pair or not at all — never a half-populated spec.
        assert (spec.max_charge_power is None) == (spec.max_discharge_power is None), model


def test_catalog_covers_every_sh_family_code_in_family_map():
    """Every family-mapped SH hybrid code should have datasheet metadata (#332).

    Skips codes for very old SH variants (SH5K-V13 / SH*K6* / SH*K-*0) that predate
    the current SH-RS/RT-20/T lineup — TCzerny's template doesn't cover them
    either. This test protects against silently dropping a currently-supported
    model.
    """
    from custom_components.sungrow.modbus_registers import DEVICE_MODEL_NAMES

    modern_names: set[str] = set()
    for name in DEVICE_MODEL_NAMES.values():
        # Legacy SH*K series (SH5K-V13, SH3K6, SH4K6-30, ...) predates the modern
        # datasheet catalog — TCzerny only covers SH*RS / SH*RT / SH*T / MG*RL.
        if name.startswith(("SH5K", "SH3K6", "SH4K6")):
            continue
        # SG string family generic — 9732 currently maps to "SG3.6RS" specifically.
        modern_names.add(name)

    missing = modern_names - set(MODEL_SPECS)
    assert not missing, f"models named in DEVICE_MODEL_NAMES without catalog entries: {sorted(missing)}"
