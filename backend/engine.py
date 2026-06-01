import pydicom
import numpy as np
import scipy.ndimage as ndimage
import yaml
import os
from typing import List, Dict, Any
from .models import QAResult, QAFlag

class QAEngine:
    def __init__(self, config_path: str):
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

    def analyze_series(self, dicom_files: List[str]) -> QAResult:
        datasets = [pydicom.dcmread(f) for f in dicom_files]
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
        # Logic: Detect high-density metal implants or devices (HU > 2000) inside the body.
        # Use a more restrictive body mask (exclude skin markers) by simple perimeter erosion if needed,
        # but here we'll use the threshold from config and increase it to ignore small marker balls.
        implant_cfg = self.config.get("thresholds", {}).get("implants", {})
        metal_threshold = implant_cfg.get("metal_threshold_hu", 2000)
        metal_vol_limit = implant_cfg.get("max_volume_cc", 0.05)

        metal_voxels = (hu_volume > metal_threshold) & body_mask
        metal_volume_cc = float(np.sum(metal_voxels) * voxel_vol)
        metal_detected = metal_volume_cc > metal_vol_limit
        metal_slices = []
        if metal_detected:
            for i in range(hu_volume.shape[0]):
                if np.any(metal_voxels[i]):
                    metal_slices.append(i + 1)

        # --- Agent: AlignmentAuditor (DISABLED) ---
        max_tilt = 0.0
        tilted_slices = []

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
            "metal_volume_cc": metal_volume_cc,
            "metal_slices": metal_slices,
            "truncated_slices": truncated_slices,
            "max_tilt_deg": max_tilt,
            "tilted_slices": tilted_slices,
            "pediatric_mismatch": pediatric_mismatch,
            "pediatric_mismatch_message": pediatric_mismatch_message,
            "rescale_slope": rescale_slope,
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
        if metrics["metal_detected"]:
            slice_info = self._format_slices(metrics.get("metal_slices", []))
            flags.append(QAFlag(name="ImplantAuditor", status="CONDITIONAL", message=f"METAL_IMPLANT: High-density metal detected ({metrics['metal_volume_cc']:.2f} cc){slice_info}. Verify implant/cardiac device safety."))

        # --- AlignmentAuditor Responsibilities (DISABLED) ---

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
