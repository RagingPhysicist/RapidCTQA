"""ImplantAuditor: metal classified as internal, surface or external to the patient.

The patient is the filled body mask; an internal margin (default 10 mm) is
removed by erosion so that skin markers and objects resting on the patient are
classified as surface metal rather than implants.
"""
from typing import Any, Dict, List

import numpy as np
import scipy.ndimage as ndimage

from backend.agents.base import SeriesContext, format_slices
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "ImplantAuditor"


def _detect_skin_markers(metal_surface, metal_external, voxel_vol, marker_max_cc):
    """Find the 3-point set-up marker pattern (1 anterior, 2 lateral) per slice."""
    marker_voxels = np.zeros_like(metal_surface, dtype=bool)
    marker_slices = []
    for i in range(metal_surface.shape[0]):
        surface_and_ext = (metal_surface[i] | metal_external[i])
        if not np.any(surface_and_ext):
            continue
        labeled, num_features = ndimage.label(surface_and_ext)
        if num_features == 0:
            continue

        comp_indices = range(1, num_features + 1)
        comp_vols = ndimage.sum(surface_and_ext, labeled, comp_indices) * voxel_vol
        marker_candidates = [idx for idx, vol in zip(comp_indices, comp_vols) if vol < marker_max_cc]

        if len(marker_candidates) == 3:
            # centroids are (y, x); anterior has the smallest y
            centroids = ndimage.center_of_mass(surface_and_ext, labeled, marker_candidates)
            sorted_by_y = sorted(centroids, key=lambda c: c[0])
            ant = sorted_by_y[0]
            lat_left, lat_right = sorted(sorted_by_y[1:], key=lambda c: c[1])

            # Anterior lies between the laterals in X and above them in Y
            if lat_left[1] < ant[1] < lat_right[1] and ant[0] < min(lat_left[0], lat_right[0]):
                for idx in marker_candidates:
                    marker_voxels[i] |= (labeled == idx)
                marker_slices.append(i + 1)
    return marker_voxels, marker_slices


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    cfg = ctx.thresholds.implants
    hu_volume = ctx.hu_volume
    interior_mask = ctx.interior_mask
    voxel_vol = ctx.voxel_vol_cc

    erosion_px = int(cfg.internal_margin_mm / ctx.pixel_spacing[0])
    shrunk_mask = np.zeros_like(hu_volume, dtype=bool)
    for i in range(hu_volume.shape[0]):
        shrunk_mask[i] = ndimage.binary_erosion(interior_mask[i], iterations=erosion_px)

    all_metal_voxels = hu_volume > cfg.metal_threshold_hu
    metal_internal = all_metal_voxels & shrunk_mask
    metal_surface = all_metal_voxels & interior_mask & ~shrunk_mask
    metal_external = all_metal_voxels & ~interior_mask

    marker_voxels, marker_slices = _detect_skin_markers(
        metal_surface, metal_external, voxel_vol, cfg.marker_max_volume_cc)
    metal_surface &= ~marker_voxels
    metal_external &= ~marker_voxels
    all_metal_voxels &= ~marker_voxels

    metal_internal_cc = float(np.sum(metal_internal) * voxel_vol)
    metal_surface_cc = float(np.sum(metal_surface) * voxel_vol)
    metal_external_cc = float(np.sum(metal_external) * voxel_vol)
    limit = cfg.max_volume_cc
    metal_detected = (metal_internal_cc > limit or metal_surface_cc > limit or metal_external_cc > limit)

    def slices_with(mask):
        return [i + 1 for i in range(mask.shape[0]) if np.any(mask[i])]

    has_metal = np.any(all_metal_voxels)
    return {
        "metal_detected": metal_detected,
        "metal_volume_cc": metal_internal_cc + metal_surface_cc + metal_external_cc,
        "metal_internal_cc": metal_internal_cc,
        "metal_surface_cc": metal_surface_cc,
        "metal_external_cc": metal_external_cc,
        "metal_slices": slices_with(all_metal_voxels) if has_metal else [],
        "metal_internal_slices": slices_with(metal_internal) if has_metal else [],
        "metal_surface_slices": slices_with(metal_surface) if has_metal else [],
        "metal_external_slices": slices_with(metal_external) if has_metal else [],
        "marker_detected": len(marker_slices) > 0,
        "marker_slices": marker_slices,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    limit = t.implants.max_volume_cc
    flags = []
    if metrics.get("metal_internal_cc", 0) > limit:
        slice_info = format_slices(metrics.get("metal_internal_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"INTERNAL_METAL: High-density metal detected deep inside body ({metrics['metal_internal_cc']:.2f} cc){slice_info}. Verify implant/cardiac device safety."))

    if metrics.get("metal_surface_cc", 0) > limit:
        slice_info = format_slices(metrics.get("metal_surface_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"SURFACE_METAL: High-density metal detected on patient skin/surface ({metrics['metal_surface_cc']:.2f} cc){slice_info}. Verify if markers or external objects."))

    if metrics.get("metal_external_cc", 0) > limit:
        slice_info = format_slices(metrics.get("metal_external_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"EXTERNAL_METAL: High-density metal detected outside body ({metrics['metal_external_cc']:.2f} cc){slice_info}. Verify no external objects are present."))
    return flags
