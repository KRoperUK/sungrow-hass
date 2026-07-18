"""Per-model Sungrow inverter datasheet metadata (#332).

The catalog is keyed by ``device_model_code`` (case-insensitive, whitespace-trimmed)
and provides the datasheet-derived numbers our dispatch entities need to set
correct upper bounds — most notably the battery ``charge_discharge_power`` slider,
which for SH-RS single-phase hybrids can exceed the AC output rating (the SH3.0RS
tops out at 3.0 kW AC but drives 6.6 kW to the battery).

Data source: TCzerny/ha-modbus-manager Sungrow dynamic templates
(``sungrow_sg_dynamic.yaml`` + ``sungrow_shx_dynamic.yaml``, MIT), which
cross-reference the Sungrow datasheets cited inline in that repo. Rows flagged
"TODO: Verify from datasheet" in TCzerny are copied here as-is with the same
provenance note; downstream consumers must not assume the values are verified
beyond what the source project asserts.

The catalog is intentionally sparse: only fields we consume today, and only the
residential lineup. Commercial KTL / CX / HX inverters are tracked separately in
:issue:`332` and not added here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    """Datasheet metadata for a specific Sungrow inverter model.

    Fields are ``None`` when the datasheet does not apply (e.g. battery power on
    a PV-only string inverter). Callers must therefore treat any of the ``max_*``
    fields as optional and fall back to a conservative default.
    """

    phases: int
    mppt_count: int
    string_count: int
    max_ac_output_power: int  # W — inverter's AC nameplate rating
    max_current: int  # A — per phase for three-phase, single value for RS
    # Battery-side limits (SH hybrids only). None on SG string inverters.
    max_charge_power: int | None = None
    max_discharge_power: int | None = None


# Every SG-RS single-phase hybrid (SH*RS 3.0..6.0) shares the same battery power
# schedule (6.6 kW charge / 6.6 kW discharge). Factored out to keep the table
# compact and to make datasheet updates a one-line change if a family revision
# lifts the limit.
_SH_RS_LOW_BATTERY = (6600, 6600)
_SH_RS_HIGH_BATTERY = (10600, 10600)


MODEL_SPECS: dict[str, ModelSpec] = {
    # --- SG-RS single-phase string (no battery) ---
    "SG3.0RS": ModelSpec(phases=1, mppt_count=2, string_count=1, max_ac_output_power=3000, max_current=14),
    "SG3.6RS": ModelSpec(phases=1, mppt_count=2, string_count=1, max_ac_output_power=3680, max_current=16),
    "SG4.0RS": ModelSpec(phases=1, mppt_count=2, string_count=1, max_ac_output_power=4000, max_current=18),
    "SG5.0RS": ModelSpec(phases=1, mppt_count=2, string_count=1, max_ac_output_power=5000, max_current=23),
    "SG6.0RS": ModelSpec(phases=1, mppt_count=2, string_count=1, max_ac_output_power=6000, max_current=27),
    "SG8.0RS": ModelSpec(phases=1, mppt_count=3, string_count=1, max_ac_output_power=8000, max_current=36),
    "SG10RS": ModelSpec(phases=1, mppt_count=3, string_count=1, max_ac_output_power=10000, max_current=45),
    # --- SG-RT three-phase string (no battery) ---
    "SG3.0RT": ModelSpec(phases=3, mppt_count=2, string_count=1, max_ac_output_power=3000, max_current=5),
    "SG4.0RT": ModelSpec(phases=3, mppt_count=2, string_count=1, max_ac_output_power=4000, max_current=6),
    "SG5.0RT": ModelSpec(phases=3, mppt_count=2, string_count=1, max_ac_output_power=5000, max_current=8),
    "SG6.0RT": ModelSpec(phases=3, mppt_count=2, string_count=1, max_ac_output_power=6000, max_current=10),
    "SG7.0RT": ModelSpec(phases=3, mppt_count=2, string_count=3, max_ac_output_power=7000, max_current=12),
    "SG8.0RT": ModelSpec(phases=3, mppt_count=2, string_count=3, max_ac_output_power=8000, max_current=13),
    "SG10RT": ModelSpec(phases=3, mppt_count=2, string_count=3, max_ac_output_power=10000, max_current=17),
    "SG12RT": ModelSpec(phases=3, mppt_count=2, string_count=3, max_ac_output_power=12000, max_current=20),
    "SG15RT": ModelSpec(phases=3, mppt_count=2, string_count=2, max_ac_output_power=15000, max_current=25),
    "SG20RT": ModelSpec(phases=3, mppt_count=2, string_count=2, max_ac_output_power=20000, max_current=32),
    # --- SH-RS single-phase hybrid (battery) ---
    "SH3.0RS": ModelSpec(1, 2, 1, 3000, 14, *_SH_RS_LOW_BATTERY),
    "SH3.6RS": ModelSpec(1, 2, 1, 3680, 16, *_SH_RS_LOW_BATTERY),
    "SH4.0RS": ModelSpec(1, 2, 1, 4000, 18, *_SH_RS_LOW_BATTERY),
    "SH5.0RS": ModelSpec(1, 2, 1, 5000, 23, *_SH_RS_LOW_BATTERY),
    "SH6.0RS": ModelSpec(1, 2, 1, 6000, 27, *_SH_RS_LOW_BATTERY),
    "SH8.0RS": ModelSpec(1, 4, 1, 8000, 36, *_SH_RS_HIGH_BATTERY),
    "SH10RS": ModelSpec(1, 4, 1, 10600, 45, *_SH_RS_HIGH_BATTERY),
    # --- SH-RT three-phase hybrid (battery). RT / RT-20 / -V112 / -V122 share
    # the same power schedule per Sungrow's datasheet family map.
    **{
        model: ModelSpec(
            phases=3,
            mppt_count=2 if not model.startswith(("SH12", "SH15", "SH20", "SH25")) else 3,
            string_count=2 if not model.startswith(("SH8", "SH10")) else 3,
            max_ac_output_power=ac,
            max_current=cur,
            max_charge_power=charge,
            max_discharge_power=discharge,
        )
        for model, ac, cur, charge, discharge in [
            ("SH5.0RT", 5000, 8, 7500, 6000),
            ("SH5.0RT-20", 5000, 8, 7500, 6000),
            ("SH5.0RT-V112", 5000, 8, 7500, 6000),
            ("SH5.0RT-V122", 5000, 8, 7500, 6000),
            ("SH6.0RT", 6000, 10, 9000, 7200),
            ("SH6.0RT-20", 6000, 10, 9000, 7200),
            ("SH6.0RT-V112", 6000, 10, 9000, 7200),
            ("SH6.0RT-V122", 6000, 10, 9000, 7200),
            ("SH8.0RT", 8000, 13, 10600, 10600),
            ("SH8.0RT-20", 8000, 13, 10600, 10600),
            ("SH8.0RT-V112", 8000, 13, 10600, 10600),
            ("SH8.0RT-V122", 8000, 13, 10600, 10600),
            ("SH10RT", 10000, 17, 10600, 10600),
            ("SH10RT-20", 10000, 17, 10600, 10600),
            ("SH10RT-V112", 10000, 17, 10600, 10600),
            ("SH10RT-V122", 10000, 17, 10600, 10600),
            # SH*T commercial three-phase hybrids. Battery limits scale with AC rating.
            ("SH5T", 5000, 8, 7500, 6000),
            ("SH6T", 6000, 10, 9000, 7200),
            ("SH8T", 8000, 13, 10600, 10600),
            ("SH10T", 10000, 17, 10600, 10600),
            ("SH12T", 12000, 20, 12000, 12000),
            ("SH15T", 15000, 25, 15000, 15000),
            ("SH20T", 20000, 32, 20000, 20000),
            ("SH25T", 25000, 40, 25000, 25000),
            # MG hybrid — battery limits estimated from RT scaling; TCzerny flags
            # these as unverified against datasheet. Kept for coverage; downstream
            # entities still clamp to the resolved ceiling.
            ("MG5RL", 5000, 8, 7500, 6000),
            ("MG6RL", 6000, 10, 9000, 7200),
            ("MG8RL", 8000, 13, 10600, 10600),
            ("MG10RL", 10000, 17, 10600, 10600),
        ]
    },
}


def spec_for(model_code: str | None) -> ModelSpec | None:
    """Return the :class:`ModelSpec` for a device model code, or ``None`` if unknown.

    Lookup is case-insensitive with leading/trailing whitespace stripped. Callers
    should treat ``None`` as "no datasheet metadata" and fall back to their
    existing conservative defaults — this preserves the current behaviour for
    every model not yet in the catalog.
    """
    if not model_code:
        return None
    key = str(model_code).strip().upper()
    if not key:
        return None
    return MODEL_SPECS.get(key)
