import logging
import os
import pydicom
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional, Tuple

from backend.models import QAResult, QAFlag, ScreenQAResult
from backend.qa_config import QAConfig, load_qa_config
from backend.status import QAStatus, screen_flags, series_verdict
from backend.utils import segment_patient_and_accessories
from backend.agents import AGENTS, implants
from backend.agents.base import SeriesContext, format_slices
from backend.agents.alignment import estimate_roll

CT_IMAGE_STORAGE = '1.2.840.10008.5.1.4.1.1.2'
RT_STRUCTURE_SET_STORAGE = '1.2.840.10008.5.1.4.1.1.481.3'

# Set to 1 to stop the engine from creating its own SegmentationService when
# none was injected (the test suite does this so it never launches a real
# TotalSegmentator run). An injected service is always used.
DISABLE_TOTALSEGMENTATOR_ENV = "RAPIDCTQA_DISABLE_TOTALSEGMENTATOR"

logger = logging.getLogger(__name__)


class QAEngine:
    def __init__(self, config_path: str, storage_dir: Optional[str] = None, segmentation_service: Any = None):
        self.config: QAConfig = load_qa_config(config_path)
        self.storage_dir = storage_dir
        self.segmentation_service = segmentation_service

    @property
    def thresholds(self):
        """Default thresholds (no protocol overrides applied)."""
        return self.config.thresholds

    def thresholds_for(self, protocol: Optional[str]):
        return self.config.thresholds_for(protocol)

    def _determine_true_patient_roll(self, pixel_array, body_mask=None):
        return estimate_roll(pixel_array, self.thresholds.alignment, body_mask=body_mask)

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
        z_steps = np.diff(sorted(float(ds.ImagePositionPatient[2]) for ds in datasets))
        z_steps = z_steps[z_steps > 0]
        slice_spacing_mm = float(np.median(z_steps)) if z_steps.size else float(datasets[0].SliceThickness)
        voxel_vol_cc = (pixel_spacing[0] * pixel_spacing[1] * float(datasets[0].SliceThickness)) / 1000.0

        # Shared masks: filled patient body (no couch / devices) and accessories.
        # TotalSegmentator's body mask is preferred; the rule-based mask is the fallback.
        masks = self._totalsegmentator_masks(datasets, hu_volume)
        used_totalsegmentator = masks is not None
        if masks is None:
            masks = segment_patient_and_accessories(
                hu_volume,
                tissue_threshold_hu=-300,
                pixel_spacing=pixel_spacing
            )
        patient_body_mask, accessory_table_mask = masks
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
            thresholds=self.thresholds_for(protocol),
            used_totalsegmentator=used_totalsegmentator,
            slice_spacing_mm=slice_spacing_mm,
        )

    def _segmentation_service_for(self, datasets: List[pydicom.Dataset]):
        if self.segmentation_service is not None:
            return self.segmentation_service
        if os.environ.get(DISABLE_TOTALSEGMENTATOR_ENV) == "1":
            return None
        # No service injected: use the storage folder the series was read from
        fn = getattr(datasets[0], 'filename', None)
        if fn:
            storage_dir = os.path.dirname(os.path.dirname(fn))
            if storage_dir and os.path.isdir(storage_dir):
                try:
                    from backend.segmentation import SegmentationService
                    return SegmentationService(storage_dir=storage_dir)
                except Exception:
                    return None
        return None

    def _totalsegmentator_masks(self, datasets, hu_volume) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """(body, accessories) masks from TotalSegmentator, or None to fall back."""
        seg_service = self._segmentation_service_for(datasets)
        if not (seg_service and seg_service.is_available):
            return None
        series_uid = datasets[0].SeriesInstanceUID
        try:
            seg_service.run_body_segmentation(series_uid=series_uid, task="body", fast=True, device="cpu")
            ts_mask = seg_service.load_body_mask(
                series_uid=series_uid, task="body", datasets=datasets, target_shape=hu_volume.shape)
        except Exception as exc:
            logger.warning("TotalSegmentator failed or unavailable for series %s: %s. "
                           "Falling back to rule-based body segmentation.", series_uid, exc)
            return None
        if ts_mask is None or ts_mask.shape != hu_volume.shape or not np.any(ts_mask):
            return None
        return ts_mask, (hu_volume > -500) & ~ts_mask

    def _compute_metrics(self, datasets: List[pydicom.Dataset], protocol: str = "Unknown") -> Dict[str, Any]:
        ctx = self._build_context(datasets, protocol)
        metrics: Dict[str, Any] = {"protocol_overrides_applied": self.config.matching_overrides(protocol)}
        for agent in AGENTS:
            metrics.update(agent.compute(ctx))
        return metrics

    def with_metal_policy(self, result: QAResult, reference_uid: Optional[str]) -> QAResult:
        """Result with ImplantAuditor flags for a 4DCT phase.

        ``reference_uid`` set and different from this series: metal is
        evaluated once per group on that reference phase, so this phase
        reports a single INFO flag. ``None``: the normal per-class flags.
        The verdict is recomputed; metrics are kept.
        """
        defer = reference_uid is not None and reference_uid != result.series_uid
        metrics = dict(result.metrics)
        if defer:
            metrics["metal_evaluated_on"] = reference_uid
            metal_flags = [QAFlag(name=implants.NAME, status=QAStatus.INFO, message=(
                f"Metal evaluated once per 4DCT group on reference phase {reference_uid}"))]
        else:
            metrics.pop("metal_evaluated_on", None)
            if "metal_internal_cc" not in metrics:
                return result  # e.g. "No valid CT image slices" result
            metal_flags = implants.evaluate(metrics, self.thresholds_for(metrics.get("protocol")))

        others = [f for f in result.flags if f.name != implants.NAME]
        first = next((i for i, f in enumerate(result.flags) if f.name == implants.NAME), len(others))
        flags = others[:first] + metal_flags + others[first:]
        return result.model_copy(update={
            "metrics": metrics,
            "flags": flags,
            "status": series_verdict(f.status for f in flags),
        })

    def screen_view(self, result: QAResult) -> ScreenQAResult:
        """Result for on-screen display: hidden (passing) checks are left out and
        counted in ``passed_checks``. The PDF uses the full result."""
        display = self.config.display
        visible = screen_flags(result.flags, display.hidden_statuses)
        return ScreenQAResult(
            **result.model_dump(exclude={"flags"}),
            flags=visible,
            passed_checks=len(result.flags) - len(visible),
            show_passed_summary=display.show_passed_summary,
        )

    def _format_slices(self, slices: List[int]) -> str:
        return format_slices(slices)

    def _evaluate_rules(self, metrics: Dict[str, Any]) -> List[QAFlag]:
        """One flag per check, every time. Only CONDITIONAL / REJECT flags are actionable."""
        thresholds = self.thresholds_for(metrics.get("protocol"))
        flags: List[QAFlag] = []
        for agent in AGENTS:
            flags.extend(agent.evaluate(metrics, thresholds))
        return flags
