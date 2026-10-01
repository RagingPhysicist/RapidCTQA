import pydicom
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

from backend.models import QAResult, QAFlag
from backend.qa_config import QAConfig, load_qa_config
from backend.status import QAStatus, series_verdict
from backend.utils import segment_patient_and_accessories
from backend.agents import AGENTS
from backend.agents.base import SeriesContext, format_slices
from backend.agents.alignment import determine_true_patient_roll

CT_IMAGE_STORAGE = '1.2.840.10008.5.1.4.1.1.2'
RT_STRUCTURE_SET_STORAGE = '1.2.840.10008.5.1.4.1.1.481.3'


class QAEngine:
    def __init__(self, config_path: str):
        self.config: QAConfig = load_qa_config(config_path)

    @property
    def thresholds(self):
        return self.config.thresholds

    def _determine_true_patient_roll(self, pixel_array, hu_threshold=None, angular_resolution=None):
        cfg = self.thresholds.alignment
        overrides = {}
        if hu_threshold is not None:
            overrides["hu_floor"] = hu_threshold
        if angular_resolution is not None:
            overrides["angular_step_deg"] = angular_resolution
        return determine_true_patient_roll(pixel_array, cfg.model_copy(update=overrides))

    def _extract_reference_point(self, rtss: pydicom.Dataset) -> Optional[Dict[str, Any]]:
        """Extract reference point or isocenter coordinates from RT Structure Set."""
        if not hasattr(rtss, 'ROIContourSequence') or not hasattr(rtss, 'StructureSetROISequence'):
            return None

        roi_map = {ss_roi.ROINumber: ss_roi.ROIName.upper() for ss_roi in rtss.StructureSetROISequence}

        # Flexible matching for names like "NewReferencePoint1" or "Isocenter"
        for roi_contour in rtss.ROIContourSequence:
            roi_name = roi_map.get(roi_contour.ReferencedROINumber, "")
            if "REFERENCE" in roi_name or "ISOCENTER" in roi_name or "ISO" in roi_name:
                if hasattr(roi_contour, 'ContourSequence') and len(roi_contour.ContourSequence) > 0:
                    contour = roi_contour.ContourSequence[0]
                    if hasattr(contour, 'ContourData') and len(contour.ContourData) >= 3:
                        return {
                            "x": float(contour.ContourData[0]),
                            "y": float(contour.ContourData[1]),
                            "z": float(contour.ContourData[2]),
                            "name": roi_map.get(roi_contour.ReferencedROINumber, "Unknown")
                        }
        return None

    def analyze_series(self, dicom_files: List[str]) -> QAResult:
        # Parallelise IO-bound DICOM reads
        with ThreadPoolExecutor(max_workers=4) as pool:
            all_datasets = list(pool.map(pydicom.dcmread, dicom_files))

        datasets = [ds for ds in all_datasets if getattr(ds, 'SOPClassUID', '') == CT_IMAGE_STORAGE]
        rtss_datasets = [ds for ds in all_datasets if getattr(ds, 'SOPClassUID', '') == RT_STRUCTURE_SET_STORAGE]

        # Keep only axial images consistent with the first slice's matrix size
        if datasets:
            datasets.sort(key=lambda x: float(getattr(x, 'ImagePositionPatient', [0, 0, 0])[2]))
            ref_rows = getattr(datasets[0], 'Rows', 0)
            ref_cols = getattr(datasets[0], 'Columns', 0)
            datasets = [
                ds for ds in datasets
                if getattr(ds, 'Rows', 0) == ref_rows
                and getattr(ds, 'Columns', 0) == ref_cols
                and 'LOCALIZER' not in [str(t).upper() for t in getattr(ds, 'ImageType', [])]
            ]

        if not datasets:
            return QAResult(
                series_uid="Filtered",
                patient_name="N/A",
                protocol="N/A",
                status=QAStatus.REJECT,
                metrics={},
                flags=[QAFlag(name="Integrity", status=QAStatus.REJECT, message="No valid CT image slices found in series.")]
            )

        series_uid = datasets[0].SeriesInstanceUID
        patient_name = str(getattr(datasets[0], 'PatientName', 'Unknown'))

        protocol = "Unknown"
        for ds in datasets:
            p = str(getattr(ds, 'ProtocolName', 'Unknown'))
            if p != "Unknown" and p.strip() != "":
                protocol = p
                break

        metrics = self._compute_metrics(datasets, protocol=protocol)

        metrics["has_rtss"] = len(rtss_datasets) > 0
        metrics["reference_point"] = self._extract_reference_point(rtss_datasets[0]) if rtss_datasets else None

        flags = self._evaluate_rules(metrics)

        return QAResult(
            series_uid=series_uid,
            patient_name=patient_name,
            protocol=protocol,
            status=series_verdict(f.status for f in flags),
            metrics=metrics,
            flags=flags
        )

    def _build_context(self, datasets: List[pydicom.Dataset], protocol: str) -> SeriesContext:
        pixel_data = np.stack([ds.pixel_array for ds in datasets])
        rescale_slope = getattr(datasets[0], 'RescaleSlope', 1.0)
        rescale_intercept = getattr(datasets[0], 'RescaleIntercept', 0.0)
        hu_volume = pixel_data * rescale_slope + rescale_intercept

        pixel_spacing = (float(datasets[0].PixelSpacing[0]), float(datasets[0].PixelSpacing[1]))
        voxel_vol_cc = (pixel_spacing[0] * pixel_spacing[1] * float(datasets[0].SliceThickness)) / 1000.0

        # Shared masks: filled patient body (no couch / devices) and accessories
        patient_body_mask, accessory_table_mask = segment_patient_and_accessories(
            hu_volume,
            tissue_threshold_hu=-300,
            pixel_spacing=pixel_spacing
        )
        empty_slices = [i + 1 for i in range(hu_volume.shape[0]) if not np.any(patient_body_mask[i])]

        return SeriesContext(
            datasets=datasets,
            hu_volume=hu_volume,
            protocol=protocol,
            pixel_spacing=pixel_spacing,
            voxel_vol_cc=voxel_vol_cc,
            interior_mask=patient_body_mask,
            accessory_table_mask=accessory_table_mask,
            empty_slices=empty_slices,
            thresholds=self.thresholds,
        )

    def _compute_metrics(self, datasets: List[pydicom.Dataset], protocol: str = "Unknown") -> Dict[str, Any]:
        ctx = self._build_context(datasets, protocol)
        metrics: Dict[str, Any] = {}
        for agent in AGENTS:
            metrics.update(agent.compute(ctx))
        return metrics

    def _format_slices(self, slices: List[int]) -> str:
        return format_slices(slices)

    def _evaluate_rules(self, metrics: Dict[str, Any]) -> List[QAFlag]:
        flags: List[QAFlag] = []
        for agent in AGENTS:
            flags.extend(agent.evaluate(metrics, self.thresholds))
        return flags
