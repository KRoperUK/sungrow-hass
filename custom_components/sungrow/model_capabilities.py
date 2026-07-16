"""Per-model capability mapping for Sungrow inverters (#251).

Which measure points a device exposes — and under which point ID — varies by inverter
family, not just by the coarse iSolarCloud ``device_type``. The clearest example is
MPPT voltage/current: string inverters (SG-family) report it on points 5-10, while
SH-family hybrids report it on a separate 13xxx range. Historically the integration
picked the range from ``device_type`` alone, which mis-served hybrids whose typing is
ambiguous and drove the recurring "missing battery/MPPT sensor" reports (#31, #189).

This module resolves the model *family* from ``device_model_code`` (e.g. ``SG3.6RS``,
``SH10RT-20``, ``SH20T``) and exposes what that family is capable of, so the coordinator
can request the right point IDs automatically. Everything degrades gracefully: an
unrecognised model yields :data:`UNKNOWN_CAPABILITIES` (no battery assumption, empty
MPPT set), and callers fall back to the existing device-type behaviour — so no existing
install regresses.

Model-code shapes (Sungrow residential/commercial naming):
    * ``SG`` prefix -> PV string inverter, no battery.
    * ``SH`` prefix -> hybrid/storage inverter, has a battery.
    * ``...RS`` suffix -> single-phase; ``...RT`` / ``...T`` / others -> three-phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .const import ESS_MPPT_DIAGNOSTIC_POINTS, STRING_MPPT_POINTS


class ModelFamily(StrEnum):
    """Coarse inverter family resolved from a Sungrow model code."""

    SG_RS = "sg_rs"  # single-phase PV string inverter
    SG_RT = "sg_rt"  # three-phase PV string inverter (SG..RT / CX / ...)
    SH_RS = "sh_rs"  # single-phase hybrid (battery)
    SH_RT = "sh_rt"  # three-phase hybrid (battery) — SH..RT / SH..T
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelCapabilities:
    """What an inverter family can report.

    ``has_battery`` / ``phases`` are ``None`` when the family is unknown, so callers can
    distinguish "known to have no battery" (a string inverter) from "don't know".
    ``mppt_points`` is the correct point-id -> code map for this family's MPPT range, or
    an empty dict when unknown.
    """

    family: ModelFamily
    has_battery: bool | None
    phases: int | None
    mppt_points: dict[str, str]


UNKNOWN_CAPABILITIES = ModelCapabilities(
    family=ModelFamily.UNKNOWN,
    has_battery=None,
    phases=None,
    mppt_points={},
)

# A single-phase model code ends in the "RS" residential-single suffix (e.g. SG3.6RS,
# SH5.0RS). Everything else Sungrow ships in these prefixes (RT, T, CX, ...) is
# three-phase. Matched case-insensitively against the trimmed model code.
_SINGLE_PHASE_RE = re.compile(r"RS$", re.IGNORECASE)


def resolve_model_family(model_code: str | None) -> ModelFamily:
    """Resolve the :class:`ModelFamily` from a device's ``device_model_code``.

    Returns :attr:`ModelFamily.UNKNOWN` for empty/unrecognised codes.
    """
    if not model_code:
        return ModelFamily.UNKNOWN
    code = str(model_code).strip().upper()
    if not code:
        return ModelFamily.UNKNOWN
    single_phase = bool(_SINGLE_PHASE_RE.search(code))
    if code.startswith("SH"):
        return ModelFamily.SH_RS if single_phase else ModelFamily.SH_RT
    if code.startswith("SG"):
        return ModelFamily.SG_RS if single_phase else ModelFamily.SG_RT
    return ModelFamily.UNKNOWN


def resolve_capabilities(model_code: str | None) -> ModelCapabilities:
    """Resolve the full :class:`ModelCapabilities` for a device's model code."""
    family = resolve_model_family(model_code)
    match family:
        case ModelFamily.SG_RS:
            return ModelCapabilities(family, has_battery=False, phases=1, mppt_points=dict(STRING_MPPT_POINTS))
        case ModelFamily.SG_RT:
            return ModelCapabilities(family, has_battery=False, phases=3, mppt_points=dict(STRING_MPPT_POINTS))
        case ModelFamily.SH_RS:
            return ModelCapabilities(family, has_battery=True, phases=1, mppt_points=dict(ESS_MPPT_DIAGNOSTIC_POINTS))
        case ModelFamily.SH_RT:
            return ModelCapabilities(family, has_battery=True, phases=3, mppt_points=dict(ESS_MPPT_DIAGNOSTIC_POINTS))
        case _:
            return UNKNOWN_CAPABILITIES


def model_has_battery(model_code: str | None) -> bool | None:
    """Return True/False if the model family implies a battery, else None (unknown)."""
    return resolve_capabilities(model_code).has_battery


def mppt_points_for_model(model_code: str | None) -> dict[str, str]:
    """Return the MPPT point-id -> code map for the model, or {} when unknown.

    SG families use the string-inverter range (points 5-10); SH families use the hybrid
    13xxx range. An empty dict signals "unknown model" so callers fall back to the
    device-type heuristic.
    """
    return resolve_capabilities(model_code).mppt_points
