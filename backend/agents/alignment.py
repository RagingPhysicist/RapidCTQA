"""AlignmentAuditor: patient roll from bilateral mirror symmetry of the central slice.

Method: weight the largest body component by clipped HU, move its centroid to
the image centre, downsample, mirror left-right and find the rotation of the
mirror image that best correlates with the original. A body rolled by θ has
a mirror image rolled by -θ, so the best rotation is 2θ.

Sign convention (unchanged from earlier versions): positive roll = clockwise
as displayed (image row 0 at the top). ``scipy.ndimage.rotate(img, +a)``
turns the image counter-clockwise on screen and is reported as roll -a.

The estimate is reported as unreliable (INFO, never an alert) when the
correlation is low or the best angle sits at the edge of the search range.
"""
from typing import Any, Dict, List, Optional

import numpy as np
import scipy.ndimage as ndimage

from backend.agents.base import SeriesContext
from backend.models import QAFlag
from backend.qa_config import AlignmentThresholds, Thresholds
from backend.status import QAStatus
from backend.utils import segment_patient_body_only

NAME = "AlignmentAuditor"


def _largest_component(mask: np.ndarray) -> np.ndarray:
    labeled, n = ndimage.label(mask)
    if n == 0:
        return mask.astype(bool)
    sizes = ndimage.sum(mask, labeled, range(1, n + 1))
    return labeled == (1 + int(np.argmax(sizes)))


def _block_mean(img: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return img
    h, w = (img.shape[0] // factor) * factor, (img.shape[1] // factor) * factor
    return img[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt(float(a @ a) * float(b @ b))
    return float(a @ b) / denom if denom > 0 else 0.0


def estimate_roll(slice_hu: np.ndarray, cfg: AlignmentThresholds, body_mask: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """Roll of one axial slice. Returns roll_deg, correlation, reliable and a reason."""
    if body_mask is None:
        try:
            body_mask = segment_patient_body_only(slice_hu, tissue_threshold_hu=cfg.hu_floor)
        except Exception:
            body_mask = ndimage.binary_fill_holes(slice_hu > cfg.hu_floor)
    body = _largest_component(np.asarray(body_mask, dtype=bool))
    if not np.any(body):
        return {"roll_deg": 0.0, "correlation": 0.0, "reliable": False, "reason": "no body on central slice"}

    # Background is 0, tissue weighted by clipped HU above the floor
    weight = (np.clip(slice_hu, cfg.hu_floor, cfg.hu_ceiling) - cfg.hu_floor) * body
    cy, cx = ndimage.center_of_mass(weight)
    H, W = weight.shape
    weight = ndimage.shift(weight, ((H - 1) / 2.0 - cy, (W - 1) / 2.0 - cx), order=1, cval=0.0)
    weight = _block_mean(weight, cfg.downsample)
    mirrored = weight[:, ::-1]

    original = weight.ravel()
    angles = np.arange(-cfg.search_range_deg, cfg.search_range_deg + 1e-9, cfg.step_deg)
    scores = np.array([
        _correlation(original, ndimage.rotate(mirrored, a, reshape=False, order=1, cval=0.0).ravel())
        for a in angles
    ])
    i = int(np.argmax(scores))
    best = float(angles[i])
    # Sub-step refinement: vertex of the parabola through the peak and its neighbours
    if 0 < i < len(scores) - 1:
        c0, c1, c2 = scores[i - 1], scores[i], scores[i + 1]
        curvature = c0 - 2 * c1 + c2
        if curvature < 0:
            best += cfg.step_deg * 0.5 * (c0 - c2) / curvature
    correlation = float(scores[i])

    reason = ""
    if abs(best) >= cfg.search_range_deg - cfg.edge_margin_deg:
        reason = f"best match at search limit (±{cfg.search_range_deg / 2:g}° roll)"
    elif correlation < cfg.min_correlation:
        reason = f"symmetry correlation {correlation:.2f} below {cfg.min_correlation:g}"
    return {
        "roll_deg": round(-best / 2.0, 2),  # see sign convention in the module docstring
        "correlation": round(correlation, 4),
        "reliable": not reason,
        "reason": reason,
    }


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    mid_idx = len(ctx.datasets) // 2
    body = ctx.interior_mask[mid_idx] if np.any(ctx.interior_mask[mid_idx]) else None
    roll = estimate_roll(ctx.hu_volume[mid_idx], ctx.thresholds.alignment, body_mask=body)
    return {
        "roll_deg": roll["roll_deg"],
        "roll_correlation": roll["correlation"],
        "roll_reliable": roll["reliable"],
        "roll_unreliable_reason": roll["reason"],
        "roll_slice": mid_idx + 1,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    cfg = t.alignment
    if "roll_deg" not in metrics:
        return [QAFlag(name=NAME, status=QAStatus.SKIPPED, message="Patient roll not evaluated")]

    roll = metrics["roll_deg"]
    corr = metrics.get("roll_correlation", 0.0)
    where = f" (Slice {metrics['roll_slice']})" if metrics.get("roll_slice") else ""
    if not metrics.get("roll_reliable", True):
        return [QAFlag(name=NAME, status=QAStatus.INFO, message=(
            f"Patient roll estimate unreliable: {metrics.get('roll_unreliable_reason', '')}{where}"))]

    limits = f"(info >{cfg.info_deg:g}°, alert >{cfg.conditional_deg:g}°, symmetry {corr:.0%})"
    if abs(roll) > cfg.conditional_deg:
        status, label = QAStatus.CONDITIONAL, "ROLL_ALERT: Patient rotation detected"
    elif abs(roll) > cfg.info_deg:
        status, label = QAStatus.INFO, "Patient roll"
    else:
        status, label = QAStatus.ACCEPT, "Patient roll"
    return [QAFlag(name=NAME, status=status, message=f"{label} {roll:+.1f}° {limits}{where}")]
