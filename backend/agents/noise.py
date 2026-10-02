"""NoiseWhisperer: background air noise and air HU calibration."""
from typing import Any, Dict, List

import numpy as np

from backend.agents.base import SeriesContext
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "NoiseWhisperer"


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    hu_volume = ctx.hu_volume
    roi = ctx.thresholds.noise.corner_roi_px

    # Background air: square ROIs in the four image corners
    corners = [
        hu_volume[:, :roi, :roi],
        hu_volume[:, :roi, -roi:],
        hu_volume[:, -roi:, :roi],
        hu_volume[:, -roi:, -roi:],
    ]
    background_air_sd = float(np.mean([np.std(c) for c in corners]))

    # Air HU estimate (1st percentile)
    valid_hu = hu_volume[hu_volume > -1500]
    air_est = float(np.percentile(valid_hu, 1)) if valid_hu.size > 0 else -1000.0

    # Centre ROI noise (informational)
    mid_z, mid_y, mid_x = [s // 2 for s in hu_volume.shape]
    center_roi = hu_volume[mid_z, mid_y - 20:mid_y + 20, mid_x - 20:mid_x + 20]

    return {
        "background_air_sd": background_air_sd,
        "center_noise_std": float(np.std(center_roi)),
        "air_hu_estimate": air_est,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    sd, sd_limit = metrics["background_air_sd"], t.noise.max_background_air_sd_hu
    air, (air_lo, air_hi) = metrics["air_hu_estimate"], t.hu.air_range
    return [
        QAFlag(name=NAME,
               status=QAStatus.CONDITIONAL if sd > sd_limit else QAStatus.ACCEPT,
               message=f"Background air noise SD {sd:.1f} HU (limit {sd_limit:g} HU)"),
        QAFlag(name=NAME,
               status=QAStatus.ACCEPT if air_lo <= air <= air_hi else QAStatus.REJECT,
               message=f"Air HU calibration {air:.1f} HU (expected {air_lo:g} to {air_hi:g} HU)"),
    ]
