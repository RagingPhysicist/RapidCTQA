"""AlignmentAuditor: patient roll from bilateral reflection symmetry (Radon sweep)."""
from typing import Any, Dict, List

import numpy as np

from backend.agents.base import SeriesContext
from backend.models import QAFlag
from backend.qa_config import AlignmentThresholds, Thresholds
from backend.status import QAStatus
from backend.utils import segment_patient_body_only

NAME = "AlignmentAuditor"


def determine_true_patient_roll(pixel_array, cfg: AlignmentThresholds) -> Dict[str, Any]:
    """
    Quantify patient roll by locating the true axis of reflection symmetry on
    one axial slice. Returns status SKIPPED when the symmetry score is below the
    confidence gate (unreadable slice), otherwise ACCEPT / CONDITIONAL against
    ``max_allowable_tilt_deg``.
    """
    try:
        from skimage.transform import radon
    except ImportError:
        return {"status": QAStatus.SKIPPED.value, "angle": 0.0, "confidence": 0.0, "message": "scikit-image not installed"}

    hu_threshold = cfg.hu_floor

    # 1. Isolate the patient's structural mass (drop couch / accessories)
    try:
        body_mask = segment_patient_body_only(pixel_array, tissue_threshold_hu=hu_threshold)
        clean_array = np.copy(pixel_array)
        clean_array[~body_mask] = -1000.0
    except Exception:
        clean_array = np.copy(pixel_array)

    clean_array[clean_array < hu_threshold] = hu_threshold

    # 2. Radon projections in a fine sweep around the vertical axis (90°)
    search_angles = np.arange(80.0, 100.0, cfg.angular_step_deg)
    sinogram = radon(clean_array, theta=search_angles, preserve_range=True)

    # 3. Angle whose projection is most symmetric about its centre
    best_angle_offset = 0.0
    max_symmetry_score = -1.0
    for i, angle in enumerate(search_angles):
        profile = sinogram[:, i]
        if np.std(profile) > 1e-6:
            correlation = float(np.corrcoef(profile, np.flip(profile))[0, 1])
            if correlation > max_symmetry_score:
                max_symmetry_score = correlation
                best_angle_offset = angle - 90.0

    # 4. Unreadable slices (extreme noise fields)
    if max_symmetry_score < cfg.symmetry_gate:
        return {"status": QAStatus.SKIPPED.value, "angle": 0.0, "confidence": max_symmetry_score}

    within_limit = abs(best_angle_offset) <= cfg.max_allowable_tilt_deg
    angle = round(-best_angle_offset, 2)  # inverted to match couch rotation convention
    return {
        "status": (QAStatus.ACCEPT if within_limit else QAStatus.CONDITIONAL).value,
        "angle": angle,
        "confidence": round(max_symmetry_score, 4),
        "metrics": f"Calculated Roll: {angle}° (Profile Similarity: {round(max_symmetry_score * 100, 2)}%)",
    }


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    mid_idx = len(ctx.datasets) // 2
    roll_info = determine_true_patient_roll(ctx.hu_volume[mid_idx], ctx.thresholds.alignment)
    return {
        "radon_roll_deg": roll_info["angle"],
        "radon_confidence": roll_info["confidence"],
        "radon_status": roll_info["status"],
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    cfg = t.alignment
    if metrics.get("radon_status") == QAStatus.SKIPPED:
        return []
    if abs(metrics["radon_roll_deg"]) > cfg.max_allowable_tilt_deg and metrics["radon_confidence"] > cfg.min_confidence:
        return [QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"ROLL_ALERT: Patient rotation detected ({metrics['radon_roll_deg']:.2f}°, Confidence: {metrics['radon_confidence']:.2%})")]
    return []
