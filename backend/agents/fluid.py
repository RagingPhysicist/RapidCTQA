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

    # Fluid (bladder): narrow range isolates urine from dense soft tissue,
    # wider range is the fallback when nothing falls inside it
    cfg = ctx.thresholds.fluid
    fluid_pixels = np.array([])
    for lo, hi in (cfg.search_range_hu, cfg.fallback_search_range_hu):
        fluid_pixels = hu_volume[(hu_volume >= lo) & (hu_volume <= hi) & body_mask]
        if fluid_pixels.size > 0:
            break
    fluid_median = float(np.median(fluid_pixels)) if fluid_pixels.size > 0 else -1000.0

    contrast_agent = str(getattr(ctx.datasets[0], 'ContrastBolusAgent', '')).strip()

    return {
        "water_hu_estimate": water_hu_est,
        "has_contrast": bool(contrast_agent),
        "fluid_median_hu": fluid_median,
        "fluid_pixels_found": fluid_pixels.size > 0,
        "rescale_slope": getattr(ctx.datasets[0], 'RescaleSlope', 1.0),
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    flags = []
    lo, hi = t.fluid.optimal_range_hu
    cond_max = t.fluid.conditional_max_hu
    # IV contrast raises fluid density, so the calibration check does not apply.
    if metrics.get("has_contrast", False):
        flags.append(QAFlag(name=NAME, status=QAStatus.SKIPPED, message="IV Contrast detected: Fluid HU calibration skipped"))
    elif not metrics.get("fluid_pixels_found", False):
        flags.append(QAFlag(name=NAME, status=QAStatus.SKIPPED, message="Fluid HU calibration skipped: no fluid-range voxels in body"))
    else:
        value = metrics["fluid_median_hu"]
        limits = f"(optimal {lo:g} to {hi:g} HU, review up to {cond_max:g} HU)"
        if lo <= value <= hi:
            flags.append(QAFlag(name=NAME, status=QAStatus.ACCEPT, message=f"Fluid density {value:.1f} HU {limits}"))
        elif hi < value <= cond_max:
            flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Fluid density variance {value:.1f} HU {limits}"))
        else:
            flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=f"HU Consistency failure: fluid {value:.1f} HU {limits}"))

    slope = metrics["rescale_slope"]
    flags.append(QAFlag(
        name=NAME,
        status=QAStatus.REJECT if slope == 0 else QAStatus.ACCEPT,
        message="Invalid RescaleSlope (0)" if slope == 0 else f"RescaleSlope {float(slope):g}"))
    return flags
