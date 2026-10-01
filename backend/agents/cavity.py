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
        if gas_volume_cc > 0:
            gas_slices = [i + 1 for i in range(hu_volume.shape[0]) if np.any(gas_voxels[i])]

    return {
        "gas_volume_cc": gas_volume_cc,
        "gas_slices": gas_slices,
        "is_pelvis_or_abdomen_scan": is_pelvis_or_abdomen_scan,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    cfg = t.gas
    gas = metrics["gas_volume_cc"]
    slice_info = format_slices(metrics.get("gas_slices", []))

    if metrics.get("is_pelvis_or_abdomen_scan"):
        if gas > cfg.leak_cc:
            return [QAFlag(name=NAME, status=QAStatus.REJECT, message=f"SEGMENTATION_LEAK: Massive non-physiological air volume detected ({gas:.1f} cc){slice_info}")]
        if gas > cfg.reject_cc:
            return [QAFlag(name=NAME, status=QAStatus.REJECT, message=f"Excessive gas volume ({gas:.1f} cc){slice_info}")]
        if gas > cfg.conditional_cc:
            return [QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Moderate gas volume ({gas:.1f} cc){slice_info}")]
        return [QAFlag(name=NAME, status=QAStatus.ACCEPT, message=f"Rectal gas volume within physiological limits ({gas:.1f} cc)")]

    if gas > cfg.conditional_cc:
        return [QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Moderate gas volume ({gas:.1f} cc){slice_info}")]
    return []
