"""FluidPhysicist: soft-tissue / fluid HU consistency and rescale metadata."""
from typing import Any, Dict, List

import numpy as np

from backend.agents.base import SeriesContext
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "FluidPhysicist"


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    hu_volume = ctx.hu_volume
    body_mask = hu_volume > -500
    water_hu_est = float(np.median(hu_volume[body_mask])) if np.any(body_mask) else 0.0

    # Fluid (bladder range)
    fluid_pixels = hu_volume[(hu_volume >= 0) & (hu_volume <= 50) & body_mask]
    fluid_median = float(np.median(fluid_pixels)) if fluid_pixels.size > 0 else -1000.0

    return {
        "water_hu_estimate": water_hu_est,
        "fluid_median_hu": fluid_median,
        "fluid_pixels_found": fluid_pixels.size > 0,
        "rescale_slope": getattr(ctx.datasets[0], 'RescaleSlope', 1.0),
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    flags = []
    # Only evaluate fluid HU calibration when fluid-range pixels exist in the scan.
    if metrics.get("fluid_pixels_found", False):
        lo, hi = t.fluid.optimal_range_hu
        value = metrics["fluid_median_hu"]
        if lo <= value <= hi:
            pass  # Optimal
        elif hi < value <= t.fluid.conditional_max_hu:
            flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Fluid density variance ({value:.1f} HU)"))
        else:
            flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=f"HU Consistency failure ({value:.1f} HU)"))

    if metrics["rescale_slope"] == 0:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message="Invalid RescaleSlope (0)"))
    return flags
