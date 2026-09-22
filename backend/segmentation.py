"""
backend/segmentation.py
========================
TotalSegmentator body-segmentation adapter.

Architecture
------------
  SegmentationService               (high-level orchestrator)
    └── TotalSegmentatorAdapter     (thin wrapper around the package)

The adapter imports TotalSegmentator programmatically (not via subprocess),
which avoids shell-injection risks and works correctly inside virtual envs.

If TotalSegmentator is not installed, SegmentationNotAvailableError is raised
with a clear install message. The caller (API layer) should surface this as
an HTTP 503 rather than a 500.

4DCT usage
----------
Pass the reference-phase series directory as ``input_dicom_dir``.
TotalSegmentator accepts a folder of DICOM slices as input directly,
so no NIfTI conversion step is required.

Run once on the reference phase only — do NOT run on all 10 phases.

Output
------
Segmentation masks are saved as NIfTI files in
``<storage_dir>/<reference_series_uid>/segmentations/<task>/``

If a previous segmentation already exists for the same series+task, it is
reused (idempotent). Pass ``force=True`` to re-run.

Temporary files
---------------
TotalSegmentator may write intermediate files to a temp directory.
The adapter creates an isolated temp dir, cleans it up on success or failure.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SegmentationNotAvailableError(RuntimeError):
    """Raised when TotalSegmentator is not installed in the current environment."""

    INSTALL_MESSAGE = (
        "TotalSegmentator is not installed. "
        "Run: pip install TotalSegmentator "
        "(also requires PyTorch ≥ 2.0.0). "
        "For CPU-only: pip install TotalSegmentator torch --index-url https://download.pytorch.org/whl/cpu"
    )

    def __init__(self, cause: Optional[BaseException] = None):
        super().__init__(self.INSTALL_MESSAGE)
        self.__cause__ = cause


class SegmentationError(RuntimeError):
    """Raised when TotalSegmentator is installed but the run itself fails."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class SegmentationResult:
    """
    Holds the paths to output segmentation files after a successful run.
    """
    series_uid: str
    task: str
    output_dir: str                     # where masks live
    mask_files: Dict[str, str] = field(default_factory=dict)  # label → .nii.gz path
    is_cached: bool = False             # True when a previous result was reused

    def to_dict(self) -> dict:
        return {
            "series_uid": self.series_uid,
            "task": self.task,
            "output_dir": self.output_dir,
            "mask_files": self.mask_files,
            "is_cached": self.is_cached,
        }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

SUPPORTED_TASKS = {"body", "total", "lung_vessels", "vertebrae_mr"}
DEFAULT_TASK = "body"
BODY_LABELS = ("body", "body_trunc", "body_extremities", "skin")


class TotalSegmentatorAdapter:
    """
    Thin wrapper around the TotalSegmentator Python API.

    Raises SegmentationNotAvailableError at construction time if the package
    cannot be imported, so the caller can detect unavailability early.
    """

    def __init__(self) -> None:
        try:
            import totalsegmentator  # noqa: F401 — verify importability only
            self._available = True
        except ImportError as exc:
            raise SegmentationNotAvailableError(cause=exc) from exc

    # ------------------------------------------------------------------

    def run(
        self,
        input_dicom_dir: str,
        output_dir: str,
        task: str = DEFAULT_TASK,
        device: str = "cpu",
        fast: bool = True,
        roi_subset: Optional[List[str]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, str]:
        """
        Execute TotalSegmentator and return a dict of {label: nifti_path}.

        Parameters
        ----------
        input_dicom_dir:
            Folder containing the DICOM slices for one series.
        output_dir:
            Destination folder for NIfTI mask files.
        task:
            TotalSegmentator task name (default: 'body').
        device:
            'cpu', 'gpu', or 'mps' (Apple Silicon).
        fast:
            Use the faster, lower-resolution model (recommended for CPU).
        roi_subset:
            Optional list of label names to restrict output (saves disk space).
        on_progress:
            Optional callable receiving progress strings for logging.
        """
        try:
            from totalsegmentator.python_api import totalsegmentator as ts_run
        except ImportError as exc:
            raise SegmentationNotAvailableError(cause=exc) from exc

        if task not in SUPPORTED_TASKS:
            logger.warning("Task '%s' not in known task list; proceeding anyway.", task)

        os.makedirs(output_dir, exist_ok=True)

        if on_progress:
            on_progress(f"Starting TotalSegmentator (task={task}, device={device}, fast={fast})")

        kwargs: dict = {
            "input": input_dicom_dir,
            "output": output_dir,
            "task": task,
            "device": device,
            "fast": fast,
            "quiet": False,
            "verbose": False,
        }

        # roi_subset is only supported in newer versions — guard it
        if roi_subset:
            kwargs["roi_subset"] = roi_subset

        try:
            ts_run(**kwargs)
        except Exception as exc:
            raise SegmentationError(
                f"TotalSegmentator failed for task='{task}': {exc}"
            ) from exc

        # Discover output masks
        mask_files: Dict[str, str] = {}
        if os.path.isdir(output_dir):
            for fname in sorted(os.listdir(output_dir)):
                if fname.endswith(".nii.gz") or fname.endswith(".nii"):
                    label = fname.replace(".nii.gz", "").replace(".nii", "")
                    mask_files[label] = os.path.join(output_dir, fname)

        if on_progress:
            on_progress(f"TotalSegmentator complete — {len(mask_files)} mask(s) written.")

        return mask_files


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SegmentationService:
    """
    High-level service that orchestrates segmentation for a CT series.

    Responsibilities:
    - Check if a cached result already exists (idempotent).
    - Delegate execution to TotalSegmentatorAdapter.
    - Manage temp dirs and cleanup.
    - Surface errors with context.
    """

    def __init__(self, storage_dir: str) -> None:
        """
        Parameters
        ----------
        storage_dir:
            Root DICOM storage directory (same as STORAGE_DIR in main.py).
            Segmentation outputs are stored as sub-directories under
            ``<storage_dir>/<series_uid>/segmentations/<task>/``.
        """
        self.storage_dir = storage_dir
        self._adapter: Optional[TotalSegmentatorAdapter] = None

    # ------------------------------------------------------------------
    # Adapter — lazy init so import errors surface on first use, not startup
    # ------------------------------------------------------------------

    def _get_adapter(self) -> TotalSegmentatorAdapter:
        if self._adapter is None:
            self._adapter = TotalSegmentatorAdapter()
        return self._adapter

    @property
    def is_available(self) -> bool:
        """Return True if TotalSegmentator is installed and importable."""
        try:
            self._get_adapter()
            return True
        except SegmentationNotAvailableError:
            return False

    # ------------------------------------------------------------------
    # Output path helpers
    # ------------------------------------------------------------------

    def output_dir_for(self, series_uid: str, task: str) -> str:
        return os.path.join(self.storage_dir, series_uid, "segmentations", task)

    def _cached_result(self, series_uid: str, task: str) -> Optional[SegmentationResult]:
        """Return a SegmentationResult from a previous run, or None."""
        out_dir = self.output_dir_for(series_uid, task)
        if not os.path.isdir(out_dir):
            return None

        mask_files: Dict[str, str] = {}
        for fname in sorted(os.listdir(out_dir)):
            if fname.endswith(".nii.gz") or fname.endswith(".nii"):
                label = fname.replace(".nii.gz", "").replace(".nii", "")
                mask_files[label] = os.path.join(out_dir, fname)

        if not mask_files:
            return None

        logger.info("Using cached segmentation for series=%s task=%s", series_uid, task)
        return SegmentationResult(
            series_uid=series_uid,
            task=task,
            output_dir=out_dir,
            mask_files=mask_files,
            is_cached=True,
        )

    # ------------------------------------------------------------------
    # Main public method
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # NIfTI Mask Loader Helper
    # ------------------------------------------------------------------

    def load_body_mask(
        self,
        series_uid: str,
        task: str = DEFAULT_TASK,
        target_shape: Optional[Tuple[int, int, int]] = None,
    ) -> Optional[np.ndarray]:
        """
        Load existing body segmentation NIfTI mask for a series as a 3D boolean numpy array.

        Parameters
        ----------
        series_uid:
            The SeriesInstanceUID.
        task:
            Task name (default: 'body').
        target_shape:
            Expected (D, H, W) volume shape for alignment verification.

        Returns
        -------
        Optional[np.ndarray]
            3D boolean numpy array of shape (D, H, W) matching DICOM volume indexing,
            or None if no segmentation is cached/available.
        """
        cached = self._cached_result(series_uid, task)
        if cached is None or not cached.mask_files:
            return None

        try:
            import nibabel as nib
        except ImportError:
            logger.warning("nibabel is not installed; cannot load NIfTI segmentation mask.")
            return None

        # Prioritise 'body' label, fallback to combining all available mask files
        mask_files_to_load = []
        if "body" in cached.mask_files:
            mask_files_to_load.append(cached.mask_files["body"])
        else:
            mask_files_to_load = list(cached.mask_files.values())

        if not mask_files_to_load:
            return None

        combined_mask: Optional[np.ndarray] = None

        for path in mask_files_to_load:
            if not os.path.isfile(path):
                continue
            try:
                nii = nib.load(path)
                data = nii.get_fdata()

                if data.ndim == 3:
                    # Convert NIfTI (W, H, D) -> DICOM volume (D, H, W)
                    if target_shape is not None:
                        D, H, W = target_shape
                        if data.shape == (W, H, D):
                            arr = np.transpose(data, (2, 1, 0))
                        elif data.shape == (D, H, W):
                            arr = data
                        elif data.shape == (H, W, D):
                            arr = np.transpose(data, (2, 0, 1))
                        else:
                            arr = np.transpose(data, (2, 1, 0))
                    else:
                        arr = np.transpose(data, (2, 1, 0))

                    mask_bool = arr > 0
                    if combined_mask is None:
                        combined_mask = mask_bool
                    else:
                        combined_mask = combined_mask | mask_bool
            except Exception as exc:
                logger.warning("Failed to read NIfTI mask %s: %s", path, exc)

        if combined_mask is not None and target_shape is not None:
            if combined_mask.shape != target_shape:
                logger.warning(
                    "Segmentation mask shape %s does not match target shape %s for series %s",
                    combined_mask.shape, target_shape, series_uid
                )

        return combined_mask

    def run_body_segmentation(
        self,
        series_uid: str,
        task: str = DEFAULT_TASK,
        device: str = "cpu",
        fast: bool = True,
        force: bool = False,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> SegmentationResult:
        """
        Run body segmentation for a single CT series.

        The series DICOM files must already reside in
        ``<storage_dir>/<series_uid>/``.

        Parameters
        ----------
        series_uid:
            The SeriesInstanceUID whose directory contains the DICOM slices.
        task:
            TotalSegmentator task (default: 'body').
        device:
            Compute device: 'cpu', 'gpu', or 'mps'.
        fast:
            Use lower-resolution fast model (strongly recommended on CPU).
        force:
            If True, re-run even if a cached result exists.
        on_progress:
            Progress callback receiving status strings.

        Returns
        -------
        SegmentationResult

        Raises
        ------
        SegmentationNotAvailableError
            TotalSegmentator not installed.
        SegmentationError
            TotalSegmentator run failed.
        FileNotFoundError
            series_uid directory does not exist.
        """
        input_dir = os.path.join(self.storage_dir, series_uid)
        if not os.path.isdir(input_dir):
            raise FileNotFoundError(
                f"Series directory not found: {input_dir}"
            )

        # Check cache first
        if not force:
            cached = self._cached_result(series_uid, task)
            if cached is not None:
                return cached

        adapter = self._get_adapter()  # raises SegmentationNotAvailableError if unavailable
        out_dir = self.output_dir_for(series_uid, task)

        # Use a temp dir alongside the final output dir; swap on success
        tmp_dir = tempfile.mkdtemp(
            prefix="totalseg_tmp_",
            dir=os.path.join(self.storage_dir, series_uid),
        )

        try:
            mask_files = adapter.run(
                input_dicom_dir=input_dir,
                output_dir=tmp_dir,
                task=task,
                device=device,
                fast=fast,
                on_progress=on_progress,
            )

            # Validate at least one mask was produced
            if not mask_files:
                raise SegmentationError(
                    f"TotalSegmentator produced no output masks in {tmp_dir}."
                )

            # Move temp output to final location (atomic-ish on same filesystem)
            if os.path.isdir(out_dir):
                shutil.rmtree(out_dir)
            shutil.move(tmp_dir, out_dir)

            # Re-map mask file paths to final location
            final_masks: Dict[str, str] = {}
            for label, tmp_path in mask_files.items():
                fname = os.path.basename(tmp_path)
                final_masks[label] = os.path.join(out_dir, fname)

            logger.info(
                "Segmentation complete: series=%s task=%s masks=%d",
                series_uid, task, len(final_masks),
            )
            return SegmentationResult(
                series_uid=series_uid,
                task=task,
                output_dir=out_dir,
                mask_files=final_masks,
                is_cached=False,
            )

        except Exception:
            # Cleanup temp dir on any failure
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            raise
