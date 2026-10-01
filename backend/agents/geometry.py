"""GeometryGuardian: FOV truncation, slice spacing, slice ordering and gantry tilt."""
from typing import Any, Dict, List

import numpy as np

from backend.agents.base import SeriesContext, format_slices, mentions_any
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "GeometryGuardian"


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    cfg = ctx.thresholds.geometry
    datasets = ctx.datasets
    hu_volume = ctx.hu_volume
    patient_body_mask = ctx.interior_mask
    accessory_table_mask = ctx.accessory_table_mask
    pixel_spacing = ctx.pixel_spacing

    # --- Slice ordering & spacing ---
    z_positions = [float(ds.ImagePositionPatient[2]) for ds in datasets]
    spacings = np.diff(sorted(z_positions))
    slice_spacing_var = float(np.max(spacings) - np.min(spacings)) if len(spacings) > 0 else 0.0
    monotonic_z = all(np.diff(z_positions) > 0) or all(np.diff(z_positions) < 0)
    duplicate_slices = len(set(z_positions)) != len(z_positions)

    # --- Protocol group for lateral truncation tolerance ---
    is_head_scan = mentions_any(cfg.head_neck_keywords, ctx.study_desc, ctx.protocol, ctx.body_part)
    is_lenient_protocol = mentions_any(cfg.lenient_protocol_keywords, ctx.protocol)
    if is_lenient_protocol:
        lateral_tol_mm = cfg.lateral_tolerance_mm.lenient
    elif is_head_scan:
        lateral_tol_mm = cfg.lateral_tolerance_mm.head_neck
    else:
        lateral_tol_mm = cfg.lateral_tolerance_mm.default

    _, H, W = hu_volume.shape
    center_y, center_x = H // 2, W // 2

    edge_buffer = cfg.edge_buffer_px
    border_mask = np.zeros((H, W), dtype=bool)
    border_mask[:edge_buffer, :] = True
    border_mask[-edge_buffer:, :] = True
    border_mask[:, :edge_buffer] = True
    border_mask[:, -edge_buffer:] = True

    truncation_error = False
    truncated_slices: List[int] = []
    tolerated_truncated_slices: List[int] = []
    accessory_truncation_detected = False
    accessory_truncated_slices: List[int] = []

    for i in range(hu_volume.shape[0]):
        # Validated empty slices bypass truncation checks
        if i + 1 in ctx.empty_slices:
            continue

        # Stage A: Patient body truncation (critical)
        trunc_y, trunc_x = np.where(patient_body_mask[i] & border_mask)
        patient_truncated_this_slice = False

        if len(trunc_y) >= cfg.min_edge_pixels:
            angles_deg = np.degrees(np.arctan2(trunc_y - center_y, trunc_x - center_x)) % 360

            critical_violation_found = False
            lateral_violation_count = 0
            max_lateral_depth_mm = 0.0

            for angle in angles_deg:
                is_right_lateral = (315.0 <= angle or angle <= 45.0)
                is_left_lateral = (135.0 <= angle <= 225.0)
                if is_right_lateral or is_left_lateral:
                    lateral_violation_count += 1
                else:
                    # Anterior or posterior core sector
                    critical_violation_found = True
                    break

            if not critical_violation_found and lateral_violation_count > 0:
                left_mask = (trunc_x < edge_buffer)
                if np.any(left_mask):
                    left_rows = trunc_y[left_mask]
                    depth_px = int(np.max(np.where(patient_body_mask[i][left_rows, :])[1]) + 1)
                    max_lateral_depth_mm = max(max_lateral_depth_mm, depth_px * pixel_spacing[0])

                right_mask = (trunc_x >= W - edge_buffer)
                if np.any(right_mask):
                    right_rows = trunc_y[right_mask]
                    depth_px = int((W - 1) - np.min(np.where(patient_body_mask[i][right_rows, :])[1]) + 1)
                    max_lateral_depth_mm = max(max_lateral_depth_mm, depth_px * pixel_spacing[0])

            if critical_violation_found or (lateral_violation_count > 0 and max_lateral_depth_mm > lateral_tol_mm):
                patient_truncated_this_slice = True
                truncation_error = True
                truncated_slices.append(i + 1)
            elif lateral_violation_count > 0:
                tolerated_truncated_slices.append(i + 1)

        # Stage B: Standalone accessory / table truncation (non-critical)
        if not patient_truncated_this_slice:
            acc_trunc_y, acc_trunc_x = np.where(accessory_table_mask[i] & border_mask)
            if len(acc_trunc_y) >= cfg.min_edge_pixels:
                # Lenient protocols: shallow accessory clipping is a tolerated
                # truncation; deep clipping escalates to a critical error.
                classified_as_tolerated = False
                if is_lenient_protocol:
                    max_acc_lateral_depth_mm = 0.0
                    left_acc = (acc_trunc_x < edge_buffer)
                    if np.any(left_acc):
                        left_rows = acc_trunc_y[left_acc]
                        right_extent = int(np.max(np.where(accessory_table_mask[i][left_rows, :])[1]) + 1)
                        max_acc_lateral_depth_mm = max(max_acc_lateral_depth_mm, right_extent * pixel_spacing[0])
                    right_acc = (acc_trunc_x >= W - edge_buffer)
                    if np.any(right_acc):
                        right_rows = acc_trunc_y[right_acc]
                        left_extent = int((W - 1) - np.min(np.where(accessory_table_mask[i][right_rows, :])[1]) + 1)
                        max_acc_lateral_depth_mm = max(max_acc_lateral_depth_mm, left_extent * pixel_spacing[0])
                    if max_acc_lateral_depth_mm < cfg.accessory_lateral_tolerance_mm:
                        tolerated_truncated_slices.append(i + 1)
                    else:
                        truncation_error = True
                        truncated_slices.append(i + 1)
                    classified_as_tolerated = True  # prevents double-counting in the accessory list
                if not classified_as_tolerated:
                    accessory_truncation_detected = True
                    accessory_truncated_slices.append(i + 1)

    return {
        "slice_spacing_var": slice_spacing_var,
        "monotonic_z": monotonic_z,
        "duplicate_slices": duplicate_slices,
        "gantry_tilt": float(getattr(datasets[0], 'GantryDetectorTilt', 0.0)),
        "truncation_detected": truncation_error or len(tolerated_truncated_slices) > 0,
        "truncation_error": truncation_error,
        "truncated_slices": truncated_slices,
        "tolerated_truncated_slices": tolerated_truncated_slices,
        "accessory_truncation_detected": accessory_truncation_detected,
        "accessory_truncated_slices": accessory_truncated_slices,
        "empty_slices": ctx.empty_slices,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    cfg = t.geometry
    flags = []

    if metrics.get("truncation_error", False):
        slice_info = format_slices(metrics.get("truncated_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=f"TRUNCATION_ERROR: Patient Body Truncation Detected (Anatomy exceeds FOV){slice_info}"))
    elif metrics.get("accessory_truncation_detected", False):
        slice_info = format_slices(metrics.get("accessory_truncated_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Accessory / Positioning Device Truncated at FOV Edge (Non-Critical Body Anatomy){slice_info}"))
    elif len(metrics.get("tolerated_truncated_slices", [])) > 0:
        slice_info = format_slices(metrics.get("tolerated_truncated_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Flared wingboard elbow clipping within clinical tolerance (<{cfg.lateral_tolerance_mm.lenient:g}mm){slice_info}"))

    if len(metrics.get("empty_slices", [])) > 0:
        slice_info = format_slices(metrics.get("empty_slices", []))
        flags.append(QAFlag(name=NAME, status=QAStatus.SKIPPED, message=f"EMPTY_SLICE: Over-range air slices bypassed{slice_info}"))

    if metrics["slice_spacing_var"] > cfg.max_slice_spacing_variation_mm:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=f"Slice spacing variation too high ({metrics['slice_spacing_var']:.2f}mm)"))

    if not metrics["monotonic_z"]:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message="Non-monotonic slice positions detected"))

    if abs(metrics["gantry_tilt"]) > cfg.max_gantry_tilt_deg:
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Gantry tilt ({metrics['gantry_tilt']}°) exceeds clinical limit"))

    if metrics["duplicate_slices"]:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message="Duplicate slice positions detected"))

    return flags
