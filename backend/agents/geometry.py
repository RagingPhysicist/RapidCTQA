"""GeometryGuardian: FOV truncation, slice spacing, slice ordering and gantry tilt.

Truncation classification per slice (contact = body mask in the FOV border ring):
  * anterior / posterior contact          -> critical (REJECT at any extent)
  * lateral-only contact, torso core also touches the border
                                          -> torso truncation; graded by z-extent
  * lateral-only contact, torso core clear -> arm / elbow (INFO)
  * accessory / couch contact only         -> INFO

The torso core is the body mask after a morphological opening (default 30 mm),
keeping the largest component: it strips arms and elbows that touch the torso
through narrow contacts or lie separately beside it.
"""
from typing import Any, Dict, List

import numpy as np
import scipy.ndimage as ndimage

from backend.agents.base import SeriesContext, format_slices
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "GeometryGuardian"


def _border_ring(H: int, W: int, edge: int) -> np.ndarray:
    ring = np.zeros((H, W), dtype=bool)
    ring[:edge, :] = True
    ring[-edge:, :] = True
    ring[:, :edge] = True
    ring[:, -edge:] = True
    return ring


def _disk(radius_px: int) -> np.ndarray:
    r = max(1, radius_px)
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def _torso_core(body_slice: np.ndarray, radius_px: int) -> np.ndarray:
    opened = ndimage.binary_opening(body_slice, structure=_disk(radius_px))
    labeled, n = ndimage.label(opened)
    if n == 0:
        return opened
    sizes = ndimage.sum(opened, labeled, range(1, n + 1))
    return labeled == (1 + int(np.argmax(sizes)))


def _has_anterior_posterior_contact(ys, xs, center_y, center_x) -> bool:
    """Any contact pixel outside the lateral sectors (315°-45° and 135°-225°)."""
    angles = np.degrees(np.arctan2(ys - center_y, xs - center_x)) % 360
    lateral = (angles >= 315.0) | (angles <= 45.0) | ((angles >= 135.0) & (angles <= 225.0))
    return bool(np.any(~lateral))


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    cfg = ctx.thresholds.geometry
    datasets = ctx.datasets
    hu_volume = ctx.hu_volume
    body = ctx.interior_mask
    accessories = ctx.accessory_table_mask

    # --- Slice ordering & spacing ---
    z_positions = [float(ds.ImagePositionPatient[2]) for ds in datasets]
    spacings = np.diff(sorted(z_positions))
    slice_spacing_var = float(np.max(spacings) - np.min(spacings)) if len(spacings) > 0 else 0.0
    monotonic_z = all(np.diff(z_positions) > 0) or all(np.diff(z_positions) < 0)
    duplicate_slices = len(set(z_positions)) != len(z_positions)

    _, H, W = hu_volume.shape
    center_y, center_x = H // 2, W // 2
    ring = _border_ring(H, W, cfg.edge_buffer_px)
    # Border length per contact pixel: mean in-plane pixel size
    px_mm = (ctx.pixel_spacing[0] + ctx.pixel_spacing[1]) / 2.0
    core_radius_px = int(round(cfg.torso_core_opening_mm / 2.0 / px_mm))

    ap_slices: List[int] = []
    torso_lateral_slices: List[int] = []
    limb_slices: List[int] = []
    accessory_slices: List[int] = []
    max_contact_mm = 0.0

    for i in range(hu_volume.shape[0]):
        if i + 1 in ctx.empty_slices:
            continue  # validated empty slices bypass truncation checks

        ys, xs = np.where(body[i] & ring)
        if len(ys) >= cfg.min_edge_pixels:
            max_contact_mm = max(max_contact_mm, len(ys) / cfg.edge_buffer_px * px_mm)
            if _has_anterior_posterior_contact(ys, xs, center_y, center_x):
                ap_slices.append(i + 1)
            elif np.any(_torso_core(body[i], core_radius_px) & ring):
                torso_lateral_slices.append(i + 1)
            else:
                limb_slices.append(i + 1)
            continue

        # Accessory / couch contact only. With a TotalSegmentator body mask
        # "accessories" are everything outside the body, so it is not reported.
        if not ctx.used_totalsegmentator and np.count_nonzero(accessories[i] & ring) >= cfg.min_edge_pixels:
            accessory_slices.append(i + 1)

    torso_z_extent_mm = len(torso_lateral_slices) * ctx.slice_spacing_mm
    truncation_error = bool(ap_slices) or torso_z_extent_mm > cfg.max_lateral_truncation_z_mm

    return {
        "slice_spacing_var": slice_spacing_var,
        "slice_spacing_mm": ctx.slice_spacing_mm,
        "monotonic_z": monotonic_z,
        "duplicate_slices": duplicate_slices,
        "gantry_tilt": float(getattr(datasets[0], 'GantryDetectorTilt', 0.0)),
        "truncation_detected": bool(ap_slices or torso_lateral_slices or limb_slices),
        "truncation_error": truncation_error,
        "truncated_slices": sorted(ap_slices + torso_lateral_slices),
        "anterior_posterior_truncated_slices": ap_slices,
        "lateral_torso_truncated_slices": torso_lateral_slices,
        "lateral_truncation_z_extent_mm": torso_z_extent_mm,
        "max_contact_length_mm": max_contact_mm,
        "tolerated_truncated_slices": limb_slices,
        "accessory_truncation_detected": bool(accessory_slices),
        "accessory_truncated_slices": accessory_slices,
        "empty_slices": ctx.empty_slices,
        "used_totalsegmentator": ctx.used_totalsegmentator,
    }


def _truncation_flag(metrics: Dict[str, Any], t: Thresholds) -> QAFlag:
    cfg = t.geometry
    ap = metrics.get("anterior_posterior_truncated_slices", [])
    torso = metrics.get("lateral_torso_truncated_slices", [])
    limbs = metrics.get("tolerated_truncated_slices", [])
    accessories = metrics.get("accessory_truncated_slices", [])
    contact = metrics.get("max_contact_length_mm", 0.0)
    z_mm = metrics.get("lateral_truncation_z_extent_mm", 0.0)
    limit = cfg.max_lateral_truncation_z_mm

    if ap:
        return QAFlag(name=NAME, status=QAStatus.REJECT, message=(
            f"TRUNCATION_ERROR: Patient Body Truncation Detected (Anatomy exceeds FOV), anterior/posterior "
            f"contact, max contact {contact:.0f} mm{format_slices(ap)}"))
    if torso:
        status = QAStatus.REJECT if z_mm > limit else QAStatus.CONDITIONAL
        prefix = "TRUNCATION_ERROR: " if status == QAStatus.REJECT else ""
        return QAFlag(name=NAME, status=status, message=(
            f"{prefix}Lateral torso truncation over {z_mm:.1f} mm z-extent (limit {limit:g} mm), "
            f"max contact {contact:.0f} mm{format_slices(torso)}"))
    if limbs:
        return QAFlag(name=NAME, status=QAStatus.INFO, message=(
            f"Arm/elbow at FOV edge, torso clear (max contact {contact:.0f} mm){format_slices(limbs)}"))
    if accessories:
        return QAFlag(name=NAME, status=QAStatus.INFO, message=(
            f"Accessory / Positioning Device Truncated at FOV Edge (Non-Critical Body Anatomy){format_slices(accessories)}"))
    return QAFlag(name=NAME, status=QAStatus.ACCEPT, message="FOV: no truncation detected")


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    cfg = t.geometry
    flags = [_truncation_flag(metrics, t)]

    empty = metrics.get("empty_slices", [])
    if empty:
        flags.append(QAFlag(name=NAME, status=QAStatus.INFO, message=f"EMPTY_SLICE: {len(empty)} over-range air slice(s) bypassed{format_slices(empty)}"))

    spacing_var = metrics["slice_spacing_var"]
    limit = cfg.max_slice_spacing_variation_mm
    flags.append(QAFlag(
        name=NAME,
        status=QAStatus.REJECT if spacing_var > limit else QAStatus.ACCEPT,
        message=f"Slice spacing variation {spacing_var:.2f} mm (limit {limit:g} mm)"))

    flags.append(QAFlag(
        name=NAME,
        status=QAStatus.ACCEPT if metrics["monotonic_z"] else QAStatus.REJECT,
        message="Slice positions monotonic" if metrics["monotonic_z"] else "Non-monotonic slice positions detected"))

    flags.append(QAFlag(
        name=NAME,
        status=QAStatus.REJECT if metrics["duplicate_slices"] else QAStatus.ACCEPT,
        message="Duplicate slice positions detected" if metrics["duplicate_slices"] else "No duplicate slice positions"))

    tilt = metrics["gantry_tilt"]
    flags.append(QAFlag(
        name=NAME,
        status=QAStatus.CONDITIONAL if abs(tilt) > cfg.max_gantry_tilt_deg else QAStatus.ACCEPT,
        message=f"Gantry tilt {tilt:g}° (limit {cfg.max_gantry_tilt_deg:g}°)"))
    return flags
