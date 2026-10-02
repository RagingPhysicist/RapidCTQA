"""CavityScout: internal gas volume in pelvis / abdomen scans."""
from typing import Any, Dict, List

import numpy as np
import scipy.ndimage as ndimage

from backend.agents.base import SeriesContext, format_slices, mentions_any
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "CavityScout"


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    cfg = ctx.thresholds.gas
    hu_volume = ctx.hu_volume
    interior_mask = ctx.interior_mask

    is_pelvis_or_abdomen_scan = mentions_any(cfg.pelvis_keywords, ctx.protocol, ctx.study_desc, ctx.body_part)

    gas_voxels = np.zeros_like(hu_volume, dtype=bool)
    gas_volume_cc = 0.0
    gas_slices: List[int] = []
    evaluated_body_cc = 0.0

    if is_pelvis_or_abdomen_scan:
        # Exclude the couch interface: bottom N mm of the patient mask
        cutoff_pixels = int(cfg.couch_exclusion_mm / ctx.pixel_spacing[1])

        # Only the inferior-most 50% of the slices along Z
        num_slices = hu_volume.shape[0]
        lower_body_slice_limit = max(1, num_slices // 2)

        for i in range(lower_body_slice_limit):
            # 1. 2D internal air (entirely surrounded by tissue) at -800 HU
            tissue_mask_slice = (hu_volume[i] >= -800)
            filled_tissue = ndimage.binary_fill_holes(tissue_mask_slice)
            internal_air_slice = filled_tissue & ~tissue_mask_slice

            # 2. Exclude the couch interface
            y_indices = np.where(interior_mask[i])[0]
            if y_indices.size > 0:
                y_cutoff = max(0, y_indices.max() - cutoff_pixels)
                search_mask = np.copy(interior_mask[i])
                search_mask[y_cutoff:, :] = False
                internal_air_slice = internal_air_slice & search_mask
            else:
                internal_air_slice = np.zeros_like(internal_air_slice, dtype=bool)

            # 3. Anatomical volume gating: drop components leaking out of the
            #    body on adjacent slices
            labeled_air, num_air_feats = ndimage.label(internal_air_slice)
            gated_air_slice = np.zeros_like(internal_air_slice, dtype=bool)
            for c in range(1, num_air_feats + 1):
                comp = (labeled_air == c)
                leak_prev = i > 0 and np.any(comp & ~interior_mask[i - 1])
                leak_next = i < num_slices - 1 and np.any(comp & ~interior_mask[i + 1])
                if not (leak_prev or leak_next):
                    gated_air_slice |= comp

            gas_voxels[i] = gated_air_slice

        gas_volume_cc = float(np.sum(gas_voxels) * ctx.voxel_vol_cc)
        evaluated_body_cc = float(np.sum(interior_mask[:lower_body_slice_limit]) * ctx.voxel_vol_cc)
        if gas_volume_cc > 0:
            gas_slices = [i + 1 for i in range(hu_volume.shape[0]) if np.any(gas_voxels[i])]

    # Body-mask sanity: an implausible gas share of the body (or no body at
    # all) means the mask leaked or failed, so the gas number is meaningless.
    gas_body_fraction = gas_volume_cc / evaluated_body_cc if evaluated_body_cc > 0 else None
    body_mask_sane = (not is_pelvis_or_abdomen_scan) or (
        gas_body_fraction is not None and gas_body_fraction <= cfg.max_gas_body_fraction)

    return {
        "gas_volume_cc": gas_volume_cc,
        "gas_slices": gas_slices,
        "is_pelvis_or_abdomen_scan": is_pelvis_or_abdomen_scan,
        "evaluated_body_volume_cc": evaluated_body_cc,
        "gas_body_fraction": gas_body_fraction,
        "body_mask_sane": body_mask_sane,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    cfg = t.gas
    if not metrics.get("is_pelvis_or_abdomen_scan"):
        return [QAFlag(name=NAME, status=QAStatus.SKIPPED, message="Gas volume not evaluated: not a pelvis/abdomen protocol")]

    gas = metrics["gas_volume_cc"]
    slice_info = format_slices(metrics.get("gas_slices", []))
    fraction = metrics.get("gas_body_fraction")
    fraction_txt = f"{fraction:.1%}" if fraction is not None else "n/a (no body voxels)"

    if not metrics.get("body_mask_sane", True):
        return [
            QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=(
                f"BODY_MASK_SANITY: gas is {fraction_txt} of the evaluated body volume "
                f"(limit {cfg.max_gas_body_fraction:.0%}); verify the body contour")),
            QAFlag(name=NAME, status=QAStatus.INFO, message=(
                f"Gas volume {gas:.1f} cc unreliable (body-mask sanity check failed){slice_info}")),
        ]

    sanity = QAFlag(name=NAME, status=QAStatus.ACCEPT, message=(
        f"Body-mask sanity: gas {fraction_txt} of evaluated body volume (limit {cfg.max_gas_body_fraction:.0%})"))
    reject_txt = f">{cfg.reject_cc:g} cc" if cfg.reject_cc is not None else "never for this protocol"
    limits = f"(info <{cfg.info_max_cc:g} cc, review >={cfg.info_max_cc:g} cc, reject {reject_txt})"

    if cfg.reject_cc is not None and gas > cfg.reject_cc:
        status, label = QAStatus.REJECT, "Excessive gas volume"
    elif gas > cfg.large_cc:
        status, label = QAStatus.CONDITIONAL, "Large gas volume"
    elif gas >= cfg.info_max_cc:
        status, label = QAStatus.CONDITIONAL, "Moderate gas volume"
    elif gas > 0:
        status, label = QAStatus.INFO, "Gas volume within physiological limits"
    else:
        status, label = QAStatus.ACCEPT, "No internal gas detected"
    gas_flag = QAFlag(name=NAME, status=status, message=f"{label}: {gas:.1f} cc {limits}{slice_info if gas > 0 else ''}")
    return [sanity, gas_flag]
