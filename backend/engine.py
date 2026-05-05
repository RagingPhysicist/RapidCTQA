import pydicom
import numpy as np
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
        z_positions = sorted([float(ds.ImagePositionPatient[2]) for ds in datasets])
        spacings = np.diff(z_positions)
        slice_spacing_var = float(np.max(spacings) - np.min(spacings)) if len(spacings) > 0 else 0.0
        monotonic_z = all(np.diff(z_positions) > 0) or all(np.diff(z_positions) < 0)
        
        # --- Agent: GeometryGuardian ---
        # Logic: Scan image matrix perimeter. If mean(edge_buffer) > skin_threshold, trigger TRUNCATION_ERROR.
        edge_buffer_px = 3
        skin_threshold = -200
        perimeter_mask = np.ones_like(hu_volume[0], dtype=bool)
        perimeter_mask[edge_buffer_px:-edge_buffer_px, edge_buffer_px:-edge_buffer_px] = False
        edge_means = [np.mean(slice[perimeter_mask]) for slice in hu_volume]
        truncation_error = any(m > skin_threshold for m in edge_means)

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
        
        # Additional: Air HU estimate
        valid_hu = hu_volume[hu_volume > -1500]
        air_est = float(np.percentile(valid_hu, 5)) if valid_hu.size > 0 else -1000.0

        # --- Agent: FluidPhysicist ---
        # Logic: Locate Bladder ROI using mid-range HU density (approx 0-50 HU).
        body_mask = hu_volume > -500
        fluid_pixels = hu_volume[(hu_volume >= 0) & (hu_volume <= 50) & body_mask]
        fluid_median = float(np.median(fluid_pixels)) if fluid_pixels.size > 0 else -1000.0
        
        # --- Agent: CavityScout ---
        voxel_vol = (float(datasets[0].PixelSpacing[0]) * float(datasets[0].PixelSpacing[1]) * float(datasets[0].SliceThickness)) / 1000.0
        gas_voxels = (hu_volume < -700) & body_mask
        gas_volume_cc = float(np.sum(gas_voxels) * voxel_vol)

        metrics = {
            "series_uid": datasets[0].SeriesInstanceUID,
            "patient_name": str(getattr(datasets[0], 'PatientName', 'Unknown')),
            "protocol": str(getattr(datasets[0], 'ProtocolName', 'Unknown')),
            "slice_count": len(datasets),
            "slice_thickness": float(datasets[0].SliceThickness),
            "slice_spacing_var": slice_spacing_var,
            "monotonic_z": monotonic_z,
            "gantry_tilt": float(getattr(datasets[0], 'GantryDetectorTilt', 0.0)),
            "truncation_detected": truncation_error,
            "background_air_sd": background_air_sd,
            "air_hu_estimate": air_est,
            "fluid_median_hu": fluid_median,
            "gas_volume_cc": gas_volume_cc,
            "rescale_slope": rescale_slope,
        }
        return metrics

    def _evaluate_rules(self, metrics: Dict[str, Any]) -> List[QAFlag]:
        flags = []
        
        # --- GeometryGuardian Responsibilities ---
        if metrics["truncation_detected"]:
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message="TRUNCATION_ERROR: Anatomy exceeds FOV"))
        
        if metrics["slice_spacing_var"] > 1.0:
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message=f"Slice spacing variation too high ({metrics['slice_spacing_var']:.2f}mm)"))
            
        if not metrics["monotonic_z"]:
            flags.append(QAFlag(name="GeometryGuardian", status="REJECT", message="Non-monotonic slice positions detected"))

        if abs(metrics["gantry_tilt"]) > 1.0:
            flags.append(QAFlag(name="GeometryGuardian", status="CONDITIONAL", message=f"Gantry tilt ({metrics['gantry_tilt']}°) exceeds clinical limit"))

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
        if metrics["gas_volume_cc"] > 50.0:
            flags.append(QAFlag(name="CavityScout", status="REJECT", message=f"Excessive gas volume ({metrics['gas_volume_cc']:.1f} cc)"))
        elif metrics["gas_volume_cc"] > 15.0:
            flags.append(QAFlag(name="CavityScout", status="CONDITIONAL", message=f"Moderate gas volume ({metrics['gas_volume_cc']:.1f} cc)"))

        # --- Integrity (Shared/Lead Oversight) ---
        if metrics["slice_thickness"] > 5.0:
            flags.append(QAFlag(name="Integrity", status="REJECT", message="Slice thickness exceeds clinical absolute limit (5mm)"))
        elif metrics["slice_thickness"] > 3.0:
            flags.append(QAFlag(name="Integrity", status="CONDITIONAL", message="Slice thickness exceeds preferred limit (3mm)"))

        return flags
