import pydicom
import numpy as np
import scipy.ndimage as ndimage
import yaml
import os
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any
from .models import QAResult, QAFlag

class QAEngine:
    def __init__(self, config_path: str):
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

    def _determine_true_patient_roll(self, pixel_array, hu_threshold=-300, angular_resolution=0.1):
        """
        Quantifies precise patient roll by locating the true axis of reflection symmetry.
        Bypasses structural inertia limitations and segmentation noise.
        """
        try:
            from skimage.transform import radon
        except ImportError:
            return {"status": "SKIPPED", "angle": 0.0, "confidence": 0.0, "message": "scikit-image not installed"}

        # 1. Clean background noise to isolate the structural mass
        clean_array = np.copy(pixel_array)
        clean_array[clean_array < hu_threshold] = hu_threshold

        # 2. Compute Radon projections around the vertical axis (90 degrees)
        # We sample a fine-grained sweep around 90° (e.g., 80.0° to 100.0°)
        search_angles = np.arange(80.0, 100.0, angular_resolution)
        sinogram = radon(clean_array, theta=search_angles, preserve_range=True)

        # 3. Find the angle where the projection is most perfectly symmetric
        best_angle_offset = 0.0
        max_symmetry_score = -1.0

        for i, angle in enumerate(search_angles):
            profile = sinogram[:, i]

            # Mirror the 1D profile to check for bilateral reflection symmetry
            mirrored_profile = np.flip(profile)

            # Calculate normalized cross-correlation between the profile and its mirror
            if np.std(profile) > 1e-6:
                correlation = float(np.corrcoef(profile, mirrored_profile)[0, 1])

                if correlation > max_symmetry_score:
                    max_symmetry_score = correlation
                    # The deviation from the true perpendicular axis (90.0°) is our roll
                    best_angle_offset = angle - 90.0

        # 4. Filter out unreadable slices (e.g., extreme noise fields)
        if max_symmetry_score < 0.90:
            return {"status": "SKIPPED", "angle": 0.0, "confidence": max_symmetry_score}

        status = "PASS" if abs(best_angle_offset) <= 1.5 else "FAIL_ROLL_DETECTED"

        return {
            "status": status,
            "angle": round(-best_angle_offset, 2),  # Invert to match standard couch rotation directions
            "confidence": round(max_symmetry_score, 4),
            "metrics": f"Calculated Roll: {round(-best_angle_offset, 2)}° (Profile Similarity: {round(max_symmetry_score * 100, 2)}%)"
        }

    def analyze_series(self, dicom_files: List[str]) -> QAResult:
        # Parallelise IO-bound DICOM reads
        with ThreadPoolExecutor() as pool:
            datasets = list(pool.map(pydicom.dcmread, dicom_files))

        # --- Fallback Filtering ---
        # Ensure we only process images with consistent dimensions (Rows/Cols)
        # and that are actually CT Image Storage (in case files were added manually to the dir)
        if datasets:
            # Pivot on the first dataset
            ref_rows = getattr(datasets[0], 'Rows', 0)
            ref_cols = getattr(datasets[0], 'Columns', 0)
            ref_sop = getattr(datasets[0], 'SOPClassUID', '')

            valid_datasets = []
            for ds in datasets:
                if (getattr(ds, 'Rows', 0) == ref_rows and
                    getattr(ds, 'Columns', 0) == ref_cols and
                    getattr(ds, 'SOPClassUID', '') == ref_sop and
                    'LOCALIZER' not in [str(t).upper() for t in getattr(ds, 'ImageType', [])]):
                    valid_datasets.append(ds)

            datasets = valid_datasets

        if not datasets:
            # Handle empty case (e.g. if all files were filtered out)
            return QAResult(
                series_uid="Filtered",
                patient_name="N/A",
                protocol="N/A",
                status="REJECT",
                metrics={},
                flags=[QAFlag(name="Integrity", status="REJECT", message="No valid CT image slices found in series.")]
            )

        series_uid = datasets[0].SeriesInstanceUID
        patient_name = str(getattr(datasets[0], 'PatientName', 'Unknown'))
        protocol = str(getattr(datasets[0], 'ProtocolName', 'Unknown'))
        
        metrics = self._compute_metrics(datasets)
        flags = self._evaluate_rules(metrics)
        
        status = "ACCEPT"
        if any(f.status == "REJECT" for f in flags):
            status = "REJECT"
        elif any(f.status == "CONDITIONAL" for f in flags):
            status = "CONDITIONAL"
            
        return QAResult(
            series_uid=series_uid,
            patient_name=patient_name,
            protocol=protocol,
            status=status,
            metrics=metrics,
            flags=flags
        )

    def _compute_metrics(self, datasets: List[pydicom.Dataset]) -> Dict[str, Any]:
        pixel_data = np.stack([ds.pixel_array for ds in datasets])
        rescale_slope = getattr(datasets[0], 'RescaleSlope', 1.0)
        rescale_intercept = getattr(datasets[0], 'RescaleIntercept', 0.0)
        hu_volume = pixel_data * rescale_slope + rescale_intercept
        
        # --- General Integrity & Geometry ---
        z_positions = [float(ds.ImagePositionPatient[2]) for ds in datasets]
        z_sorted = sorted(z_positions)
        spacings = np.diff(z_sorted)
        slice_spacing_var = float(np.max(spacings) - np.min(spacings)) if len(spacings) > 0 else 0.0
        monotonic_z = all(np.diff(z_positions) > 0) or all(np.diff(z_positions) < 0)
        duplicate_slices = len(set(z_positions)) != len(z_positions)
        
        # --- Agent: GeometryGuardian ---
        # Logic: Scan image matrix perimeter. If count(edge_buffer > skin_threshold) >= 5 on any slice, trigger TRUNCATION_ERROR.
        edge_buffer_px = 3
        skin_threshold = -200
        perimeter_mask = np.ones_like(hu_volume[0], dtype=bool)
        perimeter_mask[edge_buffer_px:-edge_buffer_px, edge_buffer_px:-edge_buffer_px] = False
        
        # Check if any slice has non-air pixels touching the perimeter
        truncation_error = False
        truncated_slices = []
        for i, slice_data in enumerate(hu_volume):
            boundary_vals = slice_data[perimeter_mask]
            # Use 5 pixels as a robust threshold to ignore isolated random noise
            if np.sum(boundary_vals > skin_threshold) >= 5:
                truncation_error = True
                truncated_slices.append(i + 1)

        # --- Agent: NoiseWhisperer ---
        # Logic: Crop 20x20px regions from the four extreme corners (Background Air).
        roi_size = 20
        corners = [
            hu_volume[:, :roi_size, :roi_size],
            hu_volume[:, :roi_size, -roi_size:],
            hu_volume[:, -roi_size:, :roi_size],
            hu_volume[:, -roi_size:, -roi_size:]
        ]
        background_air_sd = float(np.mean([np.std(c) for c in corners]))
        
        # Additional: Air HU estimate (1st percentile)
        valid_hu = hu_volume[hu_volume > -1500]
        air_est = float(np.percentile(valid_hu, 1)) if valid_hu.size > 0 else -1000.0

        # Center Noise (Center ROI)
        mid_z, mid_y, mid_x = [s // 2 for s in hu_volume.shape]
        center_roi = hu_volume[mid_z, mid_y-20:mid_y+20, mid_x-20:mid_x+20]
        center_noise_std = float(np.std(center_roi))

        # --- Agent: FluidPhysicist ---
        # Water/Fluid estimate (Soft tissue median)
        body_mask = hu_volume > -500
        water_hu_est = float(np.median(hu_volume[body_mask])) if np.any(body_mask) else 0.0
        
        # Specific Fluid (Bladder range)
        fluid_pixels = hu_volume[(hu_volume >= 0) & (hu_volume <= 50) & body_mask]
        fluid_median = float(np.median(fluid_pixels)) if fluid_pixels.size > 0 else -1000.0
        
        # --- Agent: CavityScout ---
        voxel_vol = (float(datasets[0].PixelSpacing[0]) * float(datasets[0].PixelSpacing[1]) * float(datasets[0].SliceThickness)) / 1000.0
        gas_voxels = (hu_volume < -500) & body_mask # Adjusted threshold to -500 HU
        gas_volume_cc = float(np.sum(gas_voxels) * voxel_vol)
        gas_slices = []
        if gas_volume_cc > 0:
            for i in range(hu_volume.shape[0]):
                if np.any(gas_voxels[i]):
                    gas_slices.append(i + 1)

        # --- Agent: ImplantAuditor ---
        # Build a per-slice filled body contour to accurately distinguish
        # metal that is truly inside the patient from external markers or
        # objects resting on the patient's skin (surface).
        implant_cfg = self.config.get("thresholds", {}).get("implants", {})
        metal_threshold = implant_cfg.get("metal_threshold_hu", 2000)
        metal_vol_limit = implant_cfg.get("max_volume_cc", 0.05)

        all_metal_voxels = hu_volume > metal_threshold

        # Body/Interior masks
        # 1. raw_body: Basic threshold to find patient + table
        # 2. patient_mask: Remove the table by keeping only the largest component
        # 3. interior_mask: Filled patient mask
        # 4. shrunk_mask: interior_mask eroded to ignore skin-surface objects
        patient_mask = np.zeros_like(hu_volume, dtype=bool)
        shrunk_mask = np.zeros_like(hu_volume, dtype=bool)
        interior_mask = np.zeros_like(hu_volume, dtype=bool)

        # Morphological erosion disk (~10mm)
        erosion_px = int(10.0 / float(datasets[0].PixelSpacing[0]))

        def _process_slice(i: int):
            """Compute per-slice body masks; ndimage calls release the GIL."""
            raw = hu_volume[i] > -500
            if not np.any(raw):
                return
            labeled, num_features = ndimage.label(raw)
            if num_features > 0:
                sizes = ndimage.sum(raw, labeled, range(num_features + 1))
                main_label = np.argmax(sizes[1:]) + 1
                patient_slice = (labeled == main_label)
                patient_mask[i] = patient_slice
                filled = ndimage.binary_fill_holes(patient_slice)
                interior_mask[i] = filled
                shrunk_mask[i] = ndimage.binary_erosion(filled, iterations=erosion_px)

        with ThreadPoolExecutor() as pool:
            list(pool.map(_process_slice, range(hu_volume.shape[0])))

        metal_internal = all_metal_voxels & shrunk_mask
        metal_surface = all_metal_voxels & interior_mask & ~shrunk_mask
        metal_external = all_metal_voxels & ~interior_mask

        # --- Marker Detection Heuristic ---
        # Detect 3 high-density dots on the skin (1 anterior, 2 lateral)
        marker_voxels = np.zeros_like(all_metal_voxels, dtype=bool)
        marker_slices = []
        for i in range(hu_volume.shape[0]):
            surface_and_ext = (metal_surface[i] | metal_external[i])
            if not np.any(surface_and_ext):
                continue
            labeled, num_features = ndimage.label(surface_and_ext)
            if num_features == 0:
                continue

            comp_indices = range(1, num_features + 1)
            comp_vols = ndimage.sum(surface_and_ext, labeled, comp_indices) * voxel_vol

            # Filter for small components (potential markers)
            marker_candidates = [idx for idx, vol in zip(comp_indices, comp_vols) if vol < 0.1]

            if len(marker_candidates) == 3:
                centroids = ndimage.center_of_mass(surface_and_ext, labeled, marker_candidates)
                # centroids are (y, x)
                # Sort by y (anterior is min y)
                sorted_by_y = sorted(centroids, key=lambda c: c[0])
                ant = sorted_by_y[0]
                others = sorted_by_y[1:]
                # Sort others by x to find lateral left/right
                sorted_by_x = sorted(others, key=lambda c: c[1])
                lat_left = sorted_by_x[0]
                lat_right = sorted_by_x[1]

                # Verify configuration: Anterior is between lateral in X,
                # and Lateral ones are below Anterior in Y (larger Y)
                if lat_left[1] < ant[1] < lat_right[1] and ant[0] < min(lat_left[0], lat_right[0]):
                    # It's the 3-marker pattern!
                    for idx in marker_candidates:
                        marker_voxels[i] |= (labeled == idx)
                    marker_slices.append(i + 1)

        # Exclude markers from metal masks
        metal_surface &= ~marker_voxels
        metal_external &= ~marker_voxels
        all_metal_voxels &= ~marker_voxels

        metal_internal_cc = float(np.sum(metal_internal) * voxel_vol)
        metal_surface_cc = float(np.sum(metal_surface) * voxel_vol)
        metal_external_cc = float(np.sum(metal_external) * voxel_vol)
        metal_detected = (metal_internal_cc > metal_vol_limit or
                          metal_surface_cc > metal_vol_limit or
                          metal_external_cc > metal_vol_limit)

        metal_slices = []
        metal_internal_slices = []
        metal_surface_slices = []
        metal_external_slices = []

        if np.any(all_metal_voxels):
            for i in range(hu_volume.shape[0]):
                if np.any(all_metal_voxels[i]):
                    metal_slices.append(i + 1)
                if np.any(metal_internal[i]):
                    metal_internal_slices.append(i + 1)
                if np.any(metal_surface[i]):
                    metal_surface_slices.append(i + 1)
                if np.any(metal_external[i]):
                    metal_external_slices.append(i + 1)

        # --- Agent: AlignmentAuditor (Bilateral Reflection Symmetry) ---
        align_cfg = self.config.get("thresholds", {}).get("alignment", {})
        hu_floor = align_cfg.get("hu_floor", -300)
        angular_step = align_cfg.get("angular_step_deg", 0.1)

        # Analyze central slice for symmetry
        mid_idx = len(datasets) // 2
        roll_info = self._determine_true_patient_roll(
            hu_volume[mid_idx],
            hu_threshold=hu_floor,
            angular_resolution=angular_step
        )
        # (Redundant radon processing removed, result is already in roll_info)

        # --- Pediatric Protocol Check ---
        # Both StudyDescription and ProtocolName contain "(Child)" or "(Adult)".
        # Mismatch = patient age marker doesn't match protocol marker, OR age itself contradicts marker.
        study_desc = str(getattr(datasets[0], 'StudyDescription', ''))
        protocol = str(getattr(datasets[0], 'ProtocolName', ''))
        patient_age_str = str(getattr(datasets[0], 'PatientAge', ''))

        pediatric_mismatch = False
        pediatric_mismatch_message = ""

        # Parse PatientAge (DICOM VR: AS - nnnY, nnnM, nnnW, nnnD)
        age_years = None
        if patient_age_str and len(patient_age_str) == 4:
            try:
                value = int(patient_age_str[:3])
                unit = patient_age_str[3].upper()
                if unit == 'Y':
                    age_years = value
                elif unit == 'M':
                    age_years = value / 12.0
                elif unit == 'W':
                    age_years = value / 52.17
                elif unit == 'D':
                    age_years = value / 365.25
            except ValueError:
                pass

        patient_is_child = age_years is not None and age_years < 18
        patient_is_adult = age_years is not None and age_years >= 18

        study_is_child = "(Child)" in study_desc
        study_is_adult = "(Adult)" in study_desc
        protocol_is_child = "(Child)" in protocol
        protocol_is_adult = "(Adult)" in protocol

        # Rule 1: Patient age vs Protocol/Study markers
        if patient_is_child:
            if study_is_adult or protocol_is_adult:
                pediatric_mismatch = True
                pediatric_mismatch_message = f"PEDIATRIC_MISMATCH: Child patient ({patient_age_str}) scanned with Adult protocol/study."
        elif patient_is_adult:
            if study_is_child or protocol_is_child:
                pediatric_mismatch = True
                pediatric_mismatch_message = f"PEDIATRIC_MISMATCH: Adult patient ({patient_age_str}) scanned with Child protocol/study."

        # Rule 2: Study marker vs Protocol marker mismatch (legacy check)
        if not pediatric_mismatch:
            if (study_is_child and protocol_is_adult) or (study_is_adult and protocol_is_child):
                pediatric_mismatch = True
                pediatric_mismatch_message = f"PEDIATRIC_MISMATCH: Protocol '{protocol}' does not match Study Description '{study_desc}'."

        metrics = {
            "series_uid": datasets[0].SeriesInstanceUID,
            "patient_name": str(getattr(datasets[0], 'PatientName', 'Unknown')),
            "protocol": str(getattr(datasets[0], 'ProtocolName', 'Unknown')),
            "slice_count": len(datasets),
            "slice_thickness": float(datasets[0].SliceThickness),
            "slice_spacing_var": slice_spacing_var,
            "monotonic_z": monotonic_z,
            "duplicate_slices": duplicate_slices,
            "gantry_tilt": float(getattr(datasets[0], 'GantryDetectorTilt', 0.0)),
            "truncation_detected": truncation_error,
            "background_air_sd": background_air_sd,
            "center_noise_std": center_noise_std,
            "air_hu_estimate": air_est,
            "water_hu_estimate": water_hu_est,
            "fluid_median_hu": fluid_median,
            "gas_volume_cc": gas_volume_cc,
            "gas_slices": gas_slices,
            "metal_detected": metal_detected,
            "metal_volume_cc": metal_internal_cc + metal_surface_cc + metal_external_cc,
            "metal_internal_cc": metal_internal_cc,
            "metal_surface_cc": metal_surface_cc,
            "metal_external_cc": metal_external_cc,
            "metal_slices": metal_slices,
            "metal_internal_slices": metal_internal_slices,
            "metal_surface_slices": metal_surface_slices,
            "metal_external_slices": metal_external_slices,
            "truncated_slices": truncated_slices,
            "radon_roll_deg": roll_info["angle"],
            "radon_confidence": roll_info["confidence"],
            "radon_status": roll_info["status"],
            "pediatric_mismatch": pediatric_mismatch,
            "pediatric_mismatch_message": pediatric_mismatch_message,
            "rescale_slope": rescale_slope,
            "marker_detected": len(marker_slices) > 0,
            "marker_slices": marker_slices,
        }
        return metrics

    def _format_slices(self, slices: List[int]) -> str:
        if not slices:
            return ""
        if len(slices) == 1:
            return f" (Slice {slices[0]})"

        # Group into ranges
        slices = sorted(list(set(slices)))
        ranges = []
        if not slices:
            return ""

        start = slices[0]
        end = slices[0]

        for i in range(1, len(slices)):
            if slices[i] == end + 1:
                end = slices[i]
            else:
                if start == end:
                    ranges.append(f"{start}")
                else:
                    ranges.append(f"{start}-{end}")
                start = slices[i]
                end = slices[i]

        if start == end:
            ranges.append(f"{start}")
        else:
            ranges.append(f"{start}-{end}")

        return f" (Slices {', '.join(ranges)})"

    def _evaluate_rules(self, metrics: Dict[str, Any]) -> List[QAFlag]:
        flags = []
        
        # --- GeometryGuardian Responsibilities ---
        if metrics["truncation_detected"]:
            slice_info = self._format_slices(metrics.get("truncated_slices", []))
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message=f"TRUNCATION_ERROR: Anatomy exceeds FOV{slice_info}"))
        
        if metrics["slice_spacing_var"] > 1.0:
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message=f"Slice spacing variation too high ({metrics['slice_spacing_var']:.2f}mm)"))
            
        if not metrics["monotonic_z"]:
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message="Non-monotonic slice positions detected"))

        if abs(metrics["gantry_tilt"]) > 1.0:
            flags.append(QAFlag(name="GeometryGuardian", status="CONDITIONAL", message=f"Gantry tilt ({metrics['gantry_tilt']}°) exceeds clinical limit"))

        if metrics["duplicate_slices"]:
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message="Duplicate slice positions detected"))

        # --- NoiseWhisperer Responsibilities ---
        if metrics["background_air_sd"] > 15.0:
            flags.append(QAFlag(name="NoiseWhisperer", status="CONDITIONAL", message=f"High background noise (SD: {metrics['background_air_sd']:.1f})"))
        
        if not (-1100 <= metrics["air_hu_estimate"] <= -900):
            flags.append(QAFlag(name="NoiseWhisperer", status="REJECT", message=f"Air HU calibration error ({metrics['air_hu_estimate']:.1f})"))

        # --- FluidPhysicist Responsibilities ---
        if 0 <= metrics["fluid_median_hu"] <= 35:
            pass # Optimal
        elif 35 < metrics["fluid_median_hu"] <= 45:
            flags.append(QAFlag(name="FluidPhysicist", status="CONDITIONAL", message=f"Fluid density variance ({metrics['fluid_median_hu']:.1f} HU)"))
        else:
            flags.append(QAFlag(name="FluidPhysicist", status="REJECT", message=f"HU Consistency failure ({metrics['fluid_median_hu']:.1f} HU)"))

        if metrics["rescale_slope"] == 0:
            flags.append(QAFlag(name="FluidPhysicist", status="REJECT", message="Invalid RescaleSlope (0)"))

        # --- CavityScout Responsibilities ---
        if metrics["gas_volume_cc"] > 15.0:
            slice_info = self._format_slices(metrics.get("gas_slices", []))
            if metrics["gas_volume_cc"] > 50.0:
                flags.append(QAFlag(name="CavityScout", status="REJECT", message=f"Excessive gas volume ({metrics['gas_volume_cc']:.1f} cc){slice_info}"))
            else:
                flags.append(QAFlag(name="CavityScout", status="CONDITIONAL", message=f"Moderate gas volume ({metrics['gas_volume_cc']:.1f} cc){slice_info}"))

        # --- ImplantAuditor Responsibilities ---
        metal_limit = self.config.get("thresholds", {}).get("implants", {}).get("max_volume_cc", 0.05)
        if metrics.get("metal_internal_cc", 0) > metal_limit:
            slice_info = self._format_slices(metrics.get("metal_internal_slices", []))
            flags.append(QAFlag(name="ImplantAuditor", status="CONDITIONAL", message=f"INTERNAL_METAL: High-density metal detected deep inside body ({metrics['metal_internal_cc']:.2f} cc){slice_info}. Verify implant/cardiac device safety."))

        if metrics.get("metal_surface_cc", 0) > metal_limit:
            slice_info = self._format_slices(metrics.get("metal_surface_slices", []))
            flags.append(QAFlag(name="ImplantAuditor", status="CONDITIONAL", message=f"SURFACE_METAL: High-density metal detected on patient skin/surface ({metrics['metal_surface_cc']:.2f} cc){slice_info}. Verify if markers or external objects."))

        if metrics.get("metal_external_cc", 0) > metal_limit:
            slice_info = self._format_slices(metrics.get("metal_external_slices", []))
            flags.append(QAFlag(name="ImplantAuditor", status="CONDITIONAL", message=f"EXTERNAL_METAL: High-density metal detected outside body ({metrics['metal_external_cc']:.2f} cc){slice_info}. Verify no external objects are present."))

        # --- AlignmentAuditor Responsibilities ---
        align_limit = self.config.get("thresholds", {}).get("alignment", {}).get("max_allowable_tilt_deg", 1.5)
        # Trigger ROLL_ALERT if deviation > limit AND confidence > 0.95
        if metrics.get("radon_status") != "SKIPPED":
            if abs(metrics["radon_roll_deg"]) > align_limit and metrics["radon_confidence"] > 0.95:
                flags.append(QAFlag(name="AlignmentAuditor", status="CONDITIONAL", message=f"ROLL_ALERT: Patient rotation detected ({metrics['radon_roll_deg']:.2f}°, Confidence: {metrics['radon_confidence']:.2%})"))
        if abs(metrics["radon_roll_deg"]) > align_limit and metrics["radon_confidence"] > 0.95:
            flags.append(QAFlag(name="AlignmentAuditor", status="CONDITIONAL", message=f"ROLL_ALERT: Patient rotation detected ({metrics['radon_roll_deg']:.2f}°, Confidence: {metrics['radon_confidence']:.2%})"))

        # --- Integrity (Shared/Lead Oversight) ---
        if metrics["pediatric_mismatch"]:
            flags.append(QAFlag(name="Integrity", status="REJECT", message=metrics["pediatric_mismatch_message"]))

        if metrics["slice_count"] < 5:
            flags.append(QAFlag(name="Integrity", status="REJECT", message=f"Insufficient slices for clinical series (Found: {metrics['slice_count']})"))

        if metrics["slice_thickness"] > 5.0:
            flags.append(QAFlag(name="Integrity", status="REJECT", message="Slice thickness exceeds clinical absolute limit (5mm)"))
        elif metrics["slice_thickness"] > 3.0:
            flags.append(QAFlag(name="Integrity", status="CONDITIONAL", message="Slice thickness exceeds preferred limit (3mm)"))

        return flags
