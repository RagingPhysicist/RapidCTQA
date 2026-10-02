"""CavityScout: internal gas volume in pelvis / abdomen scans.

Gas candidates are air voxels (HU below ``air_threshold_hu``) inside the
patient mask, in the inferior half of the series, above the couch interface.
They are cleaned independently of how the mask was made (TotalSegmentator
masks include air between the thighs and in the gluteal cleft):

  1. exterior_connected - 3D-connected (6-connectivity) to air touching the
     in-plane image border, or touching the first/last slice and the couch
  2. sheet_like         - thinner than ``min_thickness_mm`` (2 x max distance transform)
  3. too_shallow        - 90th-percentile in-plane depth from the nearest
     non-body / exterior air below ``min_depth_mm``
  4. cleft              - mostly inside a skin concavity: convex hull of the
     skin silhouette minus its ``cleft_closing_mm`` closing

Every candidate component is reported in ``gas_components`` with its reason.
"""
from typing import Any, Dict, List

import numpy as np
import scipy.ndimage as ndimage
from skimage.morphology import convex_hull_image, disk

from backend.agents.base import SeriesContext, format_slices, mentions_any
from backend.models import QAFlag
from backend.qa_config import GasThresholds, Thresholds
from backend.status import QAStatus

NAME = "CavityScout"
MAX_REPORTED_COMPONENTS = 50

KEPT, EXTERIOR, SHEET, SHALLOW, CLEFT = "kept", "exterior_connected", "sheet_like", "too_shallow", "cleft"


def exterior_air(air: np.ndarray, couch_mask: np.ndarray) -> np.ndarray:
    """Air that is connected to the outside of the patient in 3D.

    A component is exterior if it touches the in-plane image border, or if it
    touches the first or last slice and also borders the couch / accessories.
    """
    labels, n = ndimage.label(air)  # default structure: 6-connectivity
    if n == 0:
        return np.zeros_like(air, dtype=bool)
    border_ids = set(np.unique(labels[:, [0, -1], :])) | set(np.unique(labels[:, :, [0, -1]]))
    z_end_ids = set(np.unique(labels[[0, -1]]))
    in_plane = np.zeros((3, 3, 3), dtype=bool)
    in_plane[1] = ndimage.generate_binary_structure(2, 1)
    near_couch = ndimage.binary_dilation(couch_mask, structure=in_plane) & air
    couch_ids = set(np.unique(labels[near_couch]))
    exterior_ids = (border_ids | (z_end_ids & couch_ids)) - {0}
    return np.isin(labels, list(exterior_ids)) if exterior_ids else np.zeros_like(air, dtype=bool)


class _CleftMap:
    """Per-slice skin concavity (convex hull of the silhouette minus its closing), computed lazily."""

    def __init__(self, hu_volume, cfg: GasThresholds, pixel_mm: float):
        self.hu = hu_volume
        self.cfg = cfg
        self.radius = max(1, int(round(cfg.cleft_closing_mm / 2.0 / pixel_mm)))
        self._cache: Dict[int, np.ndarray] = {}

    def __getitem__(self, i: int) -> np.ndarray:
        if i not in self._cache:
            silhouette = ndimage.binary_fill_holes(self.hu[i] >= self.cfg.skin_hu)
            if not np.any(silhouette):
                self._cache[i] = np.zeros_like(silhouette)
            else:
                closed = ndimage.binary_closing(silhouette, structure=disk(self.radius))
                self._cache[i] = convex_hull_image(silhouette) & ~closed
        return self._cache[i]


def _classify(comp_slice_idx, comp, is_exterior, depth_map, cleft_map, cfg, sampling):
    """Reason for one candidate component (comp is a bool array over its bounding box)."""
    if is_exterior:
        return EXTERIOR, 0.0, 0.0
    padded = np.pad(comp, 1)
    thickness = 2.0 * float(ndimage.distance_transform_edt(padded, sampling=sampling).max())
    zs, ys, xs = comp_slice_idx
    depth = float(np.percentile(depth_map[zs, ys, xs], 90))
    if thickness < cfg.min_thickness_mm:
        return SHEET, depth, thickness
    if depth < cfg.min_depth_mm:
        return SHALLOW, depth, thickness
    in_cleft = sum(int(np.count_nonzero(cleft_map[z][ys[zs == z], xs[zs == z]])) for z in np.unique(zs)) / len(zs)
    if in_cleft >= cfg.cleft_fraction:
        return CLEFT, depth, thickness
    return KEPT, depth, thickness


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    cfg = ctx.thresholds.gas
    hu_volume = ctx.hu_volume
    interior_mask = ctx.interior_mask

    is_pelvis_or_abdomen_scan = mentions_any(cfg.pelvis_keywords, ctx.protocol, ctx.study_desc, ctx.body_part)

    gas_voxels = np.zeros_like(hu_volume, dtype=bool)
    gas_volume_cc = 0.0
    gas_rejected_cc = 0.0
    gas_slices: List[int] = []
    components: List[Dict[str, Any]] = []
    evaluated_body_cc = 0.0

    if is_pelvis_or_abdomen_scan:
        dy, dx = ctx.pixel_spacing
        dz = ctx.slice_spacing_mm or float(ctx.datasets[0].SliceThickness)
        num_slices = hu_volume.shape[0]
        lower = max(1, num_slices // 2)  # inferior-most 50% of the slices along Z

        air = hu_volume < cfg.air_threshold_hu
        exterior = exterior_air(air, ctx.accessory_table_mask)

        # Search region: patient mask minus the couch interface (bottom N mm per slice)
        cutoff_pixels = int(cfg.couch_exclusion_mm / dy)
        search = np.zeros_like(interior_mask, dtype=bool)
        for i in range(lower):
            y_indices = np.where(interior_mask[i])[0]
            if y_indices.size > 0:
                search[i] = interior_mask[i]
                search[i, max(0, y_indices.max() - cutoff_pixels):, :] = False
        candidates = air & search

        # In-plane depth from the nearest non-body or exterior-air voxel
        outside = ~interior_mask | exterior
        depth_map = np.zeros(hu_volume.shape, dtype=float)
        for i in range(lower):
            if np.any(candidates[i]):
                depth_map[i] = ndimage.distance_transform_edt(~outside[i], sampling=(dy, dx))
        cleft_map = _CleftMap(hu_volume, cfg, (dy + dx) / 2.0)

        labels, n = ndimage.label(candidates)
        for c, box in enumerate(ndimage.find_objects(labels), start=1):
            if box is None:
                continue
            comp = labels[box] == c
            zs, ys, xs = np.nonzero(comp)
            idx = (zs + box[0].start, ys + box[1].start, xs + box[2].start)
            reason, depth, thickness = _classify(
                idx, comp, bool(np.any(exterior[box][comp])), depth_map, cleft_map, cfg, (dz, dy, dx))
            volume = float(comp.sum() * ctx.voxel_vol_cc)
            if reason == KEPT:
                gas_voxels[idx] = True
            else:
                gas_rejected_cc += volume
            components.append({
                "volume_cc": round(volume, 3),
                "centroid": {"slice": round(float(idx[0].mean()) + 1, 1),
                             "y": round(float(idx[1].mean()), 1), "x": round(float(idx[2].mean()), 1)},
                "depth_mm": round(depth, 1),
                "thickness_mm": round(thickness, 1),
                "reason": reason,
            })

        gas_volume_cc = float(np.sum(gas_voxels) * ctx.voxel_vol_cc)
        evaluated_body_cc = float(np.sum(interior_mask[:lower]) * ctx.voxel_vol_cc)
        if gas_volume_cc > 0:
            gas_slices = [i + 1 for i in range(num_slices) if np.any(gas_voxels[i])]
        components.sort(key=lambda comp: comp["volume_cc"], reverse=True)

    # Body-mask sanity: an implausible gas share of the body (or no body at
    # all) means the mask leaked or failed, so the gas number is meaningless.
    gas_body_fraction = gas_volume_cc / evaluated_body_cc if evaluated_body_cc > 0 else None
    body_mask_sane = (not is_pelvis_or_abdomen_scan) or (
        gas_body_fraction is not None and gas_body_fraction <= cfg.max_gas_body_fraction)

    return {
        "gas_volume_cc": gas_volume_cc,
        "gas_rejected_cc": gas_rejected_cc,
        "gas_slices": gas_slices,
        "gas_components": components[:MAX_REPORTED_COMPONENTS],
        "gas_component_count": len(components),
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
