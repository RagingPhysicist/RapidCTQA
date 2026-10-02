"""Typed loader for ``ctqa.yaml``.

Every clinical limit used by the QA agents lives here, and the loader rejects
unknown keys. A typo or a section the engine does not read (for example a
``rules:`` block) is a startup error, not a silently ignored setting.

``protocol_overrides`` adjust thresholds per protocol: each entry whose
``match`` string occurs in the series' ProtocolName (case-insensitive) is
deep-merged over the defaults, in file order (later entries win). Overrides
are validated against the same schema at load time.

Defaults match the shipped ``ctqa.yaml``, so a partial config file (as used in
the tests) behaves the same as the full one for any key it leaves out.
"""
import copy
from typing import Any, Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GeometryThresholds(_Section):
    edge_buffer_px: int = 3                    # FOV border ring checked for contact
    min_edge_pixels: int = 5                   # fewer contact pixels on a slice are ignored
    torso_core_opening_mm: float = 30.0        # opening that strips arms/elbows off the torso
    max_lateral_truncation_z_mm: float = 12.0  # torso lateral contact up to this z-extent -> CONDITIONAL, longer -> REJECT
    max_slice_spacing_variation_mm: float = 1.0
    max_gantry_tilt_deg: float = 1.0


class SliceThicknessThresholds(_Section):
    nominal_mm: Optional[float] = None         # expected thickness for the protocol (None = no nominal check)
    tolerance_mm: float = 0.5                  # deviation from nominal beyond this -> CONDITIONAL
    absolute_max_mm: float = 5.0               # thicker -> REJECT


class IntegrityThresholds(_Section):
    min_slice_count: int = 5
    adult_age_years: float = 18.0


class NoiseThresholds(_Section):
    corner_roi_px: int = 20
    max_background_air_sd_hu: float = 15.0


class HUThresholds(_Section):
    air_range: Tuple[float, float] = (-1100.0, -900.0)


class FluidThresholds(_Section):
    search_range_hu: Tuple[float, float] = (0.0, 30.0)
    fallback_search_range_hu: Tuple[float, float] = (0.0, 50.0)
    optimal_range_hu: Tuple[float, float] = (0.0, 40.0)
    conditional_max_hu: float = 50.0


class GasThresholds(_Section):
    info_max_cc: float = 30.0                  # below: INFO
    large_cc: float = 75.0                     # CONDITIONAL "moderate" up to here, "large" above
    reject_cc: Optional[float] = 150.0         # above: REJECT (None = never reject, e.g. abdomen)
    max_gas_body_fraction: float = 0.10        # body-mask sanity: gas above this share of the evaluated body volume
    couch_exclusion_mm: float = 15.0
    pelvis_keywords: List[str] = ["PELVIS", "PROSTATE", "ABD", "ABDOMEN", "RECTUM", "GYN", "PELVIC"]
    # Candidate cleaning (same for TotalSegmentator and rule-based masks)
    air_threshold_hu: float = -500.0           # air = HU below this
    min_thickness_mm: float = 4.0              # thinner (2 x max distance transform) -> sheet_like
    min_depth_mm: float = 15.0                 # 90th-percentile depth below this -> too_shallow
    skin_hu: float = -200.0                    # skin silhouette used for the cleft test
    cleft_closing_mm: float = 5.0              # closing that ignores skin texture in the cleft test
    cleft_fraction: float = 0.5                # share of a component in a skin concavity -> cleft

    @model_validator(mode="after")
    def _ordered(self):
        if not self.info_max_cc <= self.large_cc:
            raise ValueError("gas thresholds must satisfy info_max_cc <= large_cc")
        if self.reject_cc is not None and self.reject_cc < self.large_cc:
            raise ValueError("gas reject_cc must be >= large_cc (or null)")
        return self


class ImplantThresholds(_Section):
    metal_threshold_hu: float = 3000.0
    internal_info_max_cc: float = 2.0          # internal metal below: INFO, at/above: CONDITIONAL
    surface_info_max_cc: float = 10.0
    external_info_max_cc: float = 5.0
    pelvis_internal_conditional_cc: float = 5.0  # pelvis scans: internal metal at/above is always CONDITIONAL
    pelvis_keywords: List[str] = ["PELVIS", "PROSTATE", "RECTUM", "GYN", "PELVIC", "BLADDER"]
    internal_margin_mm: float = 10.0
    marker_max_volume_cc: float = 0.1


class AlignmentThresholds(_Section):
    info_deg: float = 1.5                      # |roll| above: INFO
    conditional_deg: float = 3.0               # |roll| above: CONDITIONAL
    min_correlation: float = 0.90              # below: estimate unreliable (INFO)
    search_range_deg: float = 30.0             # mirror-rotation search range (roll range is half)
    step_deg: float = 0.25
    edge_margin_deg: float = 0.5               # best angle this close to the search limit: unreliable
    hu_floor: float = -300.0
    hu_ceiling: float = 300.0
    downsample: int = 4

    @model_validator(mode="after")
    def _ordered(self):
        if not self.info_deg <= self.conditional_deg:
            raise ValueError("alignment thresholds must satisfy info_deg <= conditional_deg")
        return self


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


class DisplaySettings(_Section):
    # Flag statuses left out of the on-screen report (dashboard, viewer,
    # desktop cockpit). The PDF and qa_result.json always keep every check.
    screen_hidden_statuses: List[str] = ["PASS", "ACCEPT", "INFO"]
    show_passed_summary: bool = True           # "N checks passed - full list in PDF" line

    @model_validator(mode="after")
    def _valid_statuses(self):
        from backend.status import ACTIONABLE, normalize_status
        statuses = {normalize_status(s) for s in self.screen_hidden_statuses}
        if statuses & ACTIONABLE:
            raise ValueError("display.screen_hidden_statuses cannot hide CONDITIONAL or REJECT findings")
        return self

    @property
    def hidden_statuses(self):
        from backend.status import normalize_status
        return frozenset(normalize_status(s) for s in self.screen_hidden_statuses)


class ProtocolOverride(_Section):
    match: str                                 # case-insensitive substring of ProtocolName
    thresholds: Dict[str, Any]


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class QAConfig(_Section):
    sop: Dict[str, Any] = {}
    thresholds: Thresholds = Thresholds()
    protocol_overrides: List[ProtocolOverride] = []
    display: DisplaySettings = DisplaySettings()
    _resolved: Dict[str, Thresholds] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _validate_overrides(self):
        base = self.thresholds.model_dump()
        for o in self.protocol_overrides:
            if not o.match.strip():
                raise ValueError("protocol_overrides entries need a non-empty match string")
            try:
                Thresholds.model_validate(_deep_merge(base, o.thresholds))
            except Exception as exc:
                raise ValueError(f"protocol_overrides[match={o.match!r}]: {exc}") from exc
        return self

    def matching_overrides(self, protocol: str) -> List[str]:
        p = (protocol or "").upper()
        return [o.match for o in self.protocol_overrides if o.match.strip().upper() in p]

    def thresholds_for(self, protocol: Optional[str]) -> Thresholds:
        """Thresholds for a series, with every matching protocol override applied."""
        key = (protocol or "").upper()
        if key not in self._resolved:
            merged = self.thresholds.model_dump()
            matched = [o for o in self.protocol_overrides if o.match.strip().upper() in key]
            for o in matched:
                merged = _deep_merge(merged, o.thresholds)
            self._resolved[key] = Thresholds.model_validate(merged) if matched else self.thresholds
        return self._resolved[key]


def load_qa_config(path: str) -> QAConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return QAConfig.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"Invalid QA configuration in {path}: {exc}") from exc
