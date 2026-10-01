"""Typed loader for ``ctqa.yaml``.

Every clinical limit used by the QA agents lives here, and the loader rejects
unknown keys. A typo or a section the engine does not read (for example a
``rules:`` block) is a startup error, not a silently ignored setting.

Defaults match the shipped ``ctqa.yaml``, so a partial config file (as used in
the tests) behaves the same as the full one for any key it leaves out.
"""
from typing import Any, Dict, List, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LateralToleranceMM(_Section):
    lenient: float = 15.0     # Thorax / Chest / Breast: flared wingboard elbows
    head_neck: float = 5.0
    default: float = 0.0      # Pelvis / Prostate / everything else


class GeometryThresholds(_Section):
    edge_buffer_px: int = 3
    min_edge_pixels: int = 5
    lateral_tolerance_mm: LateralToleranceMM = LateralToleranceMM()
    accessory_lateral_tolerance_mm: float = 15.0
    lenient_protocol_keywords: List[str] = ["THORAX", "CHEST", "BREAST"]
    head_neck_keywords: List[str] = ["head", "neck", "brain", "c-spine", "cspine", "cervical"]
    max_slice_spacing_variation_mm: float = 1.0
    max_gantry_tilt_deg: float = 1.0


class SliceThicknessThresholds(_Section):
    preferred_max_mm: float = 3.0
    absolute_max_mm: float = 5.0


class IntegrityThresholds(_Section):
    min_slice_count: int = 5
    adult_age_years: float = 18.0


class NoiseThresholds(_Section):
    corner_roi_px: int = 20
    max_background_air_sd_hu: float = 15.0


class HUThresholds(_Section):
    air_range: Tuple[float, float] = (-1100.0, -900.0)


class FluidThresholds(_Section):
    optimal_range_hu: Tuple[float, float] = (0.0, 35.0)
    conditional_max_hu: float = 45.0


class GasThresholds(_Section):
    conditional_cc: float = 15.0
    reject_cc: float = 50.0
    leak_cc: float = 100.0
    couch_exclusion_mm: float = 15.0
    pelvis_keywords: List[str] = ["PELVIS", "PROSTATE", "ABD", "ABDOMEN", "RECTUM", "GYN", "PELVIC"]

    @model_validator(mode="after")
    def _ordered(self):
        if not (self.conditional_cc <= self.reject_cc <= self.leak_cc):
            raise ValueError("gas thresholds must satisfy conditional_cc <= reject_cc <= leak_cc")
        return self


class ImplantThresholds(_Section):
    metal_threshold_hu: float = 3000.0
    max_volume_cc: float = 0.2
    internal_margin_mm: float = 10.0
    marker_max_volume_cc: float = 0.1


class AlignmentThresholds(_Section):
    max_allowable_tilt_deg: float = 1.5
    min_confidence: float = 0.95
    symmetry_gate: float = 0.90
    hu_floor: float = -300.0
    angular_step_deg: float = 0.1


class Thresholds(_Section):
    geometry: GeometryThresholds = GeometryThresholds()
    slice_thickness: SliceThicknessThresholds = SliceThicknessThresholds()
    integrity: IntegrityThresholds = IntegrityThresholds()
    noise: NoiseThresholds = NoiseThresholds()
    hu: HUThresholds = HUThresholds()
    fluid: FluidThresholds = FluidThresholds()
    gas: GasThresholds = GasThresholds()
    implants: ImplantThresholds = ImplantThresholds()
    alignment: AlignmentThresholds = AlignmentThresholds()


class QAConfig(_Section):
    sop: Dict[str, Any] = {}
    thresholds: Thresholds = Thresholds()


def load_qa_config(path: str) -> QAConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return QAConfig.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Invalid QA configuration in {path}: {exc}") from exc
