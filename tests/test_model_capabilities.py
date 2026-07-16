"""Tests for the per-model capability resolver (#251)."""

import pytest

from custom_components.sungrow.const import ESS_MPPT_DIAGNOSTIC_POINTS, STRING_MPPT_POINTS
from custom_components.sungrow.model_capabilities import (
    ModelFamily,
    model_has_battery,
    mppt_points_for_model,
    resolve_capabilities,
    resolve_model_family,
)


@pytest.mark.parametrize(
    ("model_code", "family"),
    [
        ("SG3.6RS", ModelFamily.SG_RS),
        ("SG5.0RS", ModelFamily.SG_RS),
        ("sg3.6rs", ModelFamily.SG_RS),  # case-insensitive
        (" SG3.6RS ", ModelFamily.SG_RS),  # trimmed
        ("SG10RT", ModelFamily.SG_RT),
        ("SG110CX", ModelFamily.SG_RT),
        ("SH5.0RS", ModelFamily.SH_RS),
        ("SH10RT-20", ModelFamily.SH_RT),
        ("SH20T", ModelFamily.SH_RT),
        ("", ModelFamily.UNKNOWN),
        ("   ", ModelFamily.UNKNOWN),  # whitespace-only
        (None, ModelFamily.UNKNOWN),
        ("WiNet-S", ModelFamily.UNKNOWN),
        ("SBR256", ModelFamily.UNKNOWN),  # battery module, not an inverter model
    ],
)
def test_resolve_model_family(model_code, family):
    """Model codes resolve to the expected family; junk resolves to UNKNOWN."""
    assert resolve_model_family(model_code) == family


def test_sg_family_is_string_inverter_no_battery():
    """SG-family: no battery, string-inverter MPPT range (points 5-10)."""
    caps = resolve_capabilities("SG3.6RS")
    assert caps.has_battery is False
    assert caps.phases == 1
    assert caps.mppt_points == STRING_MPPT_POINTS
    # Three-phase string inverter.
    assert resolve_capabilities("SG110CX").phases == 3


def test_sh_family_is_hybrid_with_battery():
    """SH-family: has a battery, hybrid MPPT range (13xxx)."""
    caps = resolve_capabilities("SH10RT-20")
    assert caps.has_battery is True
    assert caps.phases == 3
    assert caps.mppt_points == ESS_MPPT_DIAGNOSTIC_POINTS
    # Single-phase hybrid.
    assert resolve_capabilities("SH5.0RS").phases == 1


def test_unknown_model_makes_no_assumptions():
    """An unrecognised model returns None battery/phases and an empty MPPT map.

    This is what lets callers fall back to the existing device-type heuristic rather
    than regress an install with a model the map doesn't know yet.
    """
    caps = resolve_capabilities("mystery-9000")
    assert caps.family is ModelFamily.UNKNOWN
    assert caps.has_battery is None
    assert caps.phases is None
    assert caps.mppt_points == {}


def test_model_has_battery_helper():
    """model_has_battery distinguishes known-no-battery from unknown."""
    assert model_has_battery("SH10RT-20") is True
    assert model_has_battery("SG3.6RS") is False
    assert model_has_battery(None) is None
    assert model_has_battery("WiNet-S") is None


def test_mppt_points_for_model_swaps_range_by_family():
    """MPPT ids are the string range for SG and the hybrid range for SH; {} when unknown."""
    assert mppt_points_for_model("SG3.6RS") == STRING_MPPT_POINTS
    assert mppt_points_for_model("SH10RT-20") == ESS_MPPT_DIAGNOSTIC_POINTS
    assert mppt_points_for_model("nope") == {}


def test_string_and_hybrid_mppt_ranges_are_disjoint():
    """The two MPPT ranges must not share point ids, or a swap couldn't be clean."""
    assert set(STRING_MPPT_POINTS) & set(ESS_MPPT_DIAGNOSTIC_POINTS) == set()
