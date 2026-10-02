"""ImplantAuditor: metal classified as internal, surface or external to the patient.

The patient is the filled body mask; an internal margin (default 10 mm) is
removed by erosion so that skin markers and objects resting on the patient are
classified as surface metal rather than implants.
"""
from typing import Any, Dict, List

import numpy as np
import scipy.ndimage as ndimage

from backend.agents.base import SeriesContext, format_slices, mentions_any
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
    metal_detected = (metal_internal_cc >= cfg.internal_info_max_cc
                      or metal_surface_cc >= cfg.surface_info_max_cc
                      or metal_external_cc >= cfg.external_info_max_cc)

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
        "is_pelvis_scan": mentions_any(cfg.pelvis_keywords, ctx.protocol, ctx.study_desc, ctx.body_part),
    }


# (class, metrics key, label, advice, config attribute of the INFO limit)
_CLASSES = (
    ("internal", "INTERNAL_METAL", "deep inside body", "Verify implant/cardiac device safety.", "internal_info_max_cc"),
    ("surface", "SURFACE_METAL", "on patient skin/surface", "Verify if markers or external objects.", "surface_info_max_cc"),
    ("external", "EXTERNAL_METAL", "outside body", "Verify no external objects are present.", "external_info_max_cc"),
)


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    """One flag per metal class: none -> ACCEPT, below the class limit -> INFO, at/above -> CONDITIONAL.

    Pelvis scans: internal metal at/above ``pelvis_internal_conditional_cc``
    is CONDITIONAL even if a protocol override raised the internal limit.
    """
    cfg = t.implants
    flags = []
    for cls, code, where, advice, limit_attr in _CLASSES:
        volume = metrics.get(f"metal_{cls}_cc", 0.0)
        limit = getattr(cfg, limit_attr)
        slice_info = format_slices(metrics.get(f"metal_{cls}_slices", []))
        if volume <= 0:
            flags.append(QAFlag(name=NAME, status=QAStatus.ACCEPT, message=f"{code}: none detected (>{cfg.metal_threshold_hu:g} HU)"))
            continue

        conditional = volume >= limit
        limit_txt = f"limit {limit:g} cc"
        if cls == "internal" and metrics.get("is_pelvis_scan") and volume >= cfg.pelvis_internal_conditional_cc:
            conditional = True
            limit_txt += f", pelvis limit {cfg.pelvis_internal_conditional_cc:g} cc"
        message = f"{code}: High-density metal detected {where} ({volume:.2f} cc, {limit_txt}){slice_info}."
        flags.append(QAFlag(
            name=NAME,
            status=QAStatus.CONDITIONAL if conditional else QAStatus.INFO,
            message=f"{message} {advice}" if conditional else message))
    return flags
