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
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import numpy as np

from backend import mask_cache
from backend.security import is_valid_uid, safe_child_path, series_dir

CT_IMAGE_STORAGE = '1.2.840.10008.5.1.4.1.1.2'

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
        # DICOM-space mask cache: one build lock per (series, task), open memmaps, NIfTI LRU
        self._locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._memmaps: Dict[str, Tuple[tuple, np.ndarray]] = {}
        self._nifti_lru = mask_cache.NiftiLRU(max_entries=2)

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
        return safe_child_path(os.path.join(series_dir(self.storage_dir, series_uid), "segmentations"), task)

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
        datasets: Optional[List[Any]] = None,
        target_shape: Optional[Tuple[int, int, int]] = None,
    ) -> Optional[np.ndarray]:
        """
        Body segmentation mask of a series as a 3D bool array (D, H, W) in
        DICOM voxel order (CT slices sorted by z).

        Uses the persisted DICOM-space cache (see :meth:`get_body_mask_dicom_space`),
        building it on first use. Without DICOM geometry (no ``datasets`` and no
        readable headers in storage) the NIfTI is only transposed, as before.
        Returns None if no segmentation exists.
        """
        if not is_valid_uid(series_uid):
            return None
        sources = self._mask_sources(series_uid, task)
        if not sources:
            return None

        mask = self.get_body_mask_dicom_space(series_uid, task=task, datasets=datasets)
        if mask is not None:
            mask = np.array(mask, dtype=bool)
        else:
            mask = self._transposed_mask(sources, target_shape)

        if mask is not None and target_shape is not None and mask.shape != tuple(target_shape):
            logger.warning(
                "Segmentation mask shape %s does not match target shape %s for series %s",
                mask.shape, target_shape, series_uid
            )
        return mask

    # ------------------------------------------------------------------
    # DICOM-space mask cache
    # ------------------------------------------------------------------

    def _mask_sources(self, series_uid: str, task: str) -> List[str]:
        """NIfTI files that make up the body mask: 'body' if present, else every mask."""
        try:
            out_dir = self.output_dir_for(series_uid, task)
            names = sorted(os.listdir(out_dir))
        except (OSError, ValueError):
            return []
        nifti = {n.replace(".nii.gz", "").replace(".nii", ""): os.path.join(out_dir, n)
                 for n in names if n.endswith(".nii.gz") or n.endswith(".nii")}
        if "body" in nifti:
            return [nifti["body"]]
        return list(nifti.values())

    def mask_cache_path(self, series_uid: str, task: str = DEFAULT_TASK) -> str:
        return os.path.join(self.output_dir_for(series_uid, task), mask_cache.CACHE_FILENAME)

    def mask_version(self, series_uid: str, task: str = DEFAULT_TASK) -> Optional[float]:
        """Newest mtime of the mask's NIfTI files (None if not segmented). Cheap: stat only."""
        try:
            sources = self._mask_sources(series_uid, task)
            return max(os.stat(src).st_mtime for src in sources) if sources else None
        except OSError:
            return None

    def _series_lock(self, series_uid: str, task: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault((series_uid, task), threading.Lock())

    def _read_geometry(self, series_uid: str):
        """DICOM geometry of the series' CT images in storage (header reads; build path only)."""
        import glob
        import pydicom
        loaded = []
        for f in glob.glob(os.path.join(series_dir(self.storage_dir, series_uid), "*.dcm")):
            try:
                ds = pydicom.dcmread(f, stop_before_pixels=True)
                if getattr(ds, 'SOPClassUID', '') == CT_IMAGE_STORAGE:
                    loaded.append(ds)
            except Exception:
                continue
        if not loaded:
            return None
        loaded.sort(key=lambda x: float(getattr(x, 'ImagePositionPatient', [0, 0, 0])[2]))
        return mask_cache.geometry_from_datasets(loaded)

    def _open_cache(self, path: str, sources: List[str], expected_shape) -> Optional[np.ndarray]:
        """Read-only memmap of a fresh cache file, reusing an already open handle."""
        try:
            st = os.stat(path)
            signature = (st.st_mtime, st.st_size, tuple(os.stat(src).st_mtime for src in sources))
        except OSError:
            return None
        with self._locks_guard:
            hit = self._memmaps.get(path)
        if hit is not None and hit[0] == signature:
            mm = hit[1]
        elif mask_cache.cache_is_fresh(path, sources):
            mm = np.load(path, mmap_mode="r")
            with self._locks_guard:
                self._memmaps[path] = (signature, mm)
        else:
            return None
        if expected_shape is not None and tuple(mm.shape) != tuple(expected_shape):
            return None
        return mm

    def get_body_mask_dicom_space(
        self,
        series_uid: str,
        task: str = DEFAULT_TASK,
        datasets: Optional[List[Any]] = None,
        expected_shape: Optional[Tuple[int, int, int]] = None,
    ) -> Optional[np.ndarray]:
        """Read-only memmap (D, H, W) bool of the body mask in DICOM voxel space.

        Stored as ``segmentations/<task>/body_dicom.npy`` and built once
        (atomically, one builder per series) from the NIfTI. Rebuilt when a
        NIfTI is newer than the cache or the shape does not match the series.
        Returns None if there is no segmentation or no DICOM geometry.
        """
        if not is_valid_uid(series_uid):
            return None
        sources = self._mask_sources(series_uid, task)
        if not sources:
            return None
        geometry = mask_cache.geometry_from_datasets(datasets) if datasets else None
        if expected_shape is None and geometry is not None:
            expected_shape = geometry.shape
        path = self.mask_cache_path(series_uid, task)

        mm = self._open_cache(path, sources, expected_shape)
        if mm is not None:
            return mm
        with self._series_lock(series_uid, task):
            mm = self._open_cache(path, sources, expected_shape)  # built while we waited
            if mm is not None:
                return mm
            geometry = geometry or self._read_geometry(series_uid)
            if geometry is None:
                return None
            volumes = [mask_cache.load_nifti_mask(src) for src in sources]
            mask = mask_cache.resample_to_dicom(volumes, geometry.rows, geometry.columns, geometry.slices)
            mask_cache.write_atomic(path, mask)
            logger.info("Built DICOM-space mask cache %s %s", path, mask.shape)
        # Return what was just built (not re-validated: a NIfTI with a skewed
        # clock must not make this call return None)
        return np.load(path, mmap_mode="r")

    def get_mask_slice(
        self,
        series_uid: str,
        index: int,
        task: str = DEFAULT_TASK,
        expected_shape: Optional[Tuple[int, int, int]] = None,
        slice_geometry: Optional["mask_cache.SliceGeometry"] = None,
    ) -> Optional[np.ndarray]:
        """One (H, W) bool mask slice for the viewer, without blocking on a build.

        With a fresh cache this reads one slice from the memmap. Otherwise the
        cache is built in the background and this slice is resampled from an
        in-memory NIfTI (LRU of 2 series), so a request never resamples a volume.
        """
        sources = self._mask_sources(series_uid, task)
        if not sources:
            return None
        mm = self._open_cache(self.mask_cache_path(series_uid, task), sources, expected_shape)
        if mm is not None:
            return np.array(mm[index], dtype=bool) if 0 <= index < mm.shape[0] else None

        self._build_in_background(series_uid, task)
        if slice_geometry is None or expected_shape is None:
            return None
        volumes = self._nifti_lru.get(sources)
        return mask_cache.resample_to_dicom(volumes, expected_shape[1], expected_shape[2], [slice_geometry])[0]

    def _build_in_background(self, series_uid: str, task: str) -> None:
        if self._series_lock(series_uid, task).locked():
            return  # already building

        def _build():
            try:
                self.get_body_mask_dicom_space(series_uid, task=task)
            except Exception as exc:
                logger.warning("Building mask cache for %s failed: %s", series_uid, exc)
        threading.Thread(target=_build, name=f"mask-cache-{series_uid[-12:]}", daemon=True).start()

    def _transposed_mask(self, sources: List[str], target_shape) -> Optional[np.ndarray]:
        """Fallback without DICOM geometry: transpose NIfTI (W, H, D) to (D, H, W)."""
        combined = None
        for path in sources:
            try:
                data, _ = mask_cache.load_nifti_mask(path)
            except Exception as exc:
                logger.warning("Failed to read NIfTI mask %s: %s", path, exc)
                continue
            if data.ndim != 3:
                continue
            if target_shape is not None and data.shape == tuple(target_shape):
                arr = data
            else:
                arr = np.transpose(data, (2, 1, 0))
            combined = arr if combined is None else combined | arr
        return combined

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
        input_dir = series_dir(self.storage_dir, series_uid)
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
            dir=input_dir,
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
            # Build the DICOM-space cache now, so the viewer never pays for it
            try:
                self.get_body_mask_dicom_space(series_uid, task=task)
            except Exception as exc:
                logger.warning("Could not build mask cache for %s: %s", series_uid, exc)
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
