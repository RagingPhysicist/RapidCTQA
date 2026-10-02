"""Shared runtime state and the analysis pipeline used by the API routers.

Paths are always read through ``settings.<NAME>`` at call time so tests can
patch them in one place.
"""
import glob
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pydicom

from backend import settings
from backend.dicom_sender import send_dicom_series
from backend.engine import CT_IMAGE_STORAGE, DISABLE_TOTALSEGMENTATOR_ENV, QAEngine
from backend.fourdct import FourDCTGroup, detect_fourdct_groups
from backend.listener import DicomListener
from backend.logger import log_qa_result
from backend.models import QAResult
from backend.reporter import generate_pdf_report
from backend.security import is_valid_uid, series_dir
from backend.segmentation import SegmentationService
from backend.status import QAStatus
from backend.viewer_cache import SeriesView, SliceLRU


results_cache: Dict[str, QAResult] = {}
ct_files_cache: Dict[str, List[str]] = {}
results_cache_lock = threading.Lock()

# Cache of detected 4DCT groups: group_id -> FourDCTGroup. Rebuilt on each
# /api/studies call; used for group-level endpoints.
fourdct_cache: Dict[str, FourDCTGroup] = {}
fourdct_cache_lock = threading.Lock()

# Pool for concurrent series analysis (IO + CPU-bound work per series)
analysis_pool = ThreadPoolExecutor(max_workers=4)
# Separate pool for slice rendering, so scrolling never waits behind an analysis
viewer_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="viewer")

# Viewer caches: per-series file list + geometry, and decoded HU slices
series_views: Dict[str, SeriesView] = {}
slice_cache = SliceLRU(max_slices=160)

segmentation_service = SegmentationService(storage_dir=settings.STORAGE_DIR)

# RAPIDCTQA_DISABLE_TOTALSEGMENTATOR=1 keeps QA analysis on the rule-based
# body mask (development / testing); manual segmentation from the UI still works.
_qa_uses_totalsegmentator = os.environ.get(DISABLE_TOTALSEGMENTATOR_ENV) != "1"
engine = QAEngine(
    settings.QA_CONFIG_PATH,
    storage_dir=settings.STORAGE_DIR,
    segmentation_service=segmentation_service if _qa_uses_totalsegmentator else None,
)


def storage_path(series_uid: str) -> str:
    return series_dir(settings.STORAGE_DIR, series_uid)


def export_path(series_uid: str) -> str:
    return series_dir(settings.EXPORT_DIR, series_uid)


def report_path(series_uid: str) -> str:
    if not is_valid_uid(series_uid):
        raise ValueError(f"Invalid series UID: {series_uid!r}")
    return os.path.join(settings.REPORTS_DIR, f"QA_Report_{series_uid}.pdf")


def list_series_dirs() -> List[str]:
    """Series UIDs present in storage (directories whose name is a valid UID)."""
    if not os.path.isdir(settings.STORAGE_DIR):
        return []
    return [
        entry for entry in os.listdir(settings.STORAGE_DIR)
        if is_valid_uid(entry) and os.path.isdir(os.path.join(settings.STORAGE_DIR, entry))
    ]


def read_patient_id(dicom_files: List[str]) -> str:
    for f in dicom_files:
        try:
            ds = pydicom.dcmread(f, stop_before_pixels=True)
            pid = str(getattr(ds, 'PatientID', '')).strip()
            if pid:
                return pid
        except Exception:
            continue
    return "Unknown"


def on_series_received(series_uid: str):
    if not is_valid_uid(series_uid):
        print(f"Ignoring analysis request for invalid series UID {series_uid!r}")
        return
    if listener.is_ingesting(series_uid):
        print(f"Series {series_uid} is still being received; analysis will run when it is stable.")
        return

    print(f"Received series: {series_uid}. Starting analysis...")
    study_path = storage_path(series_uid)
    dicom_files = glob.glob(os.path.join(study_path, "*.dcm"))
    if not dicom_files:
        return

    result = engine.analyze_series(dicom_files)
    with results_cache_lock:
        results_cache[series_uid] = result
    # The file list may have grown since the viewer last cached it
    invalidate_viewer_caches(series_uid)

    # 4DCT: metal is evaluated once per group. Applying the group policy here
    # (before logging and export) also updates sibling phases.
    try:
        groups, _ = detect_fourdct_groups({uid: storage_path(uid) for uid in list_series_dirs()})
        apply_group_metal_policy(groups)
    except Exception as e:
        print(f"Error applying 4DCT metal policy for {series_uid}: {e}")
    with results_cache_lock:
        result = results_cache[series_uid]
    print(f"Analysis complete for {series_uid}: {result.status}")

    _persist_result(series_uid, result)

    try:
        log_qa_result(result, patient_id=read_patient_id(dicom_files))
    except Exception as e:
        print(f"Error logging QA result for {series_uid}: {e}")

    try:
        generate_pdf_report(result, report_path(series_uid))
    except Exception as e:
        print(f"Error auto-generating PDF report: {e}")

    # Auto-export accepted series
    if result.status == QAStatus.ACCEPT:
        print(f"Auto-exporting {series_uid} to TPS...")
        shutil.copytree(study_path, export_path(series_uid), dirs_exist_ok=True)
        print(f"Auto-routing accepted series {series_uid} to DICOM destinations...")
        send_dicom_series(study_path)


def _persist_result(series_uid: str, result: QAResult):
    try:
        with open(os.path.join(storage_path(series_uid), "qa_result.json"), "w", encoding="utf-8") as f:
            f.write(result.model_dump_json())
    except Exception as e:
        print(f"Error saving cached result to disk: {e}")


def apply_group_metal_policy(groups: List[FourDCTGroup]) -> List[str]:
    """Evaluate metal once per 4DCT group: only the reference phase keeps its
    ImplantAuditor flags, the other phases get one INFO flag pointing to it.

    Changed results are updated in the cache, on disk, in the problem log and
    in the PDF report. Returns the series UIDs that changed. Phases that were
    already exported or reported keep that history; a phase that becomes
    ACCEPT here is not auto-exported retroactively.
    """
    changed = []
    for group in groups:
        ref = group.reference_phase_uid or None
        for phase in group.phases:
            uid = phase.series_uid
            with results_cache_lock:
                current = results_cache.get(uid)
                if current is None:
                    continue
                updated = engine.with_metal_policy(current, ref)
                if updated == current:
                    continue
                results_cache[uid] = updated
            changed.append(uid)
            _persist_result(uid, updated)
            try:
                log_qa_result(updated, patient_id=read_patient_id(glob.glob(os.path.join(storage_path(uid), "*.dcm"))))
                generate_pdf_report(updated, report_path(uid))
            except Exception as e:
                print(f"Error updating log/report for 4DCT phase {uid}: {e}")
    return changed


def submit_series(series_uid: str):
    """Submit a series for analysis on the shared thread pool."""
    analysis_pool.submit(on_series_received, series_uid)


listener = DicomListener(
    settings.STORAGE_DIR,
    submit_series,
    stability_seconds=settings.DICOM_STABILITY_SECONDS,
    allowed_calling_aets=settings.DICOM_ALLOWED_CALLING_AETS,
    allowed_peers=settings.DICOM_ALLOWED_PEERS,
)


def series_status(series_uid: str) -> QAStatus:
    if listener.is_ingesting(series_uid):
        return QAStatus.INGESTING
    cached = results_cache.get(series_uid)
    return QAStatus(cached.status) if cached else QAStatus.PENDING


def get_series_view(series_uid: str) -> SeriesView:
    """Sorted CT files and per-slice geometry of a series, read once (headers only)."""
    with results_cache_lock:
        view = series_views.get(series_uid)
        if view is not None:
            return view

    headers = []
    for f in glob.glob(os.path.join(storage_path(series_uid), "*.dcm")):
        try:
            ds = pydicom.dcmread(f, stop_before_pixels=True)
            if ds.SOPClassUID == CT_IMAGE_STORAGE:
                headers.append((f, ds))
        except Exception:
            continue
    headers.sort(key=lambda h: float(getattr(h[1], 'ImagePositionPatient', [0, 0, 0])[2]))
    view = SeriesView.from_headers(headers)

    with results_cache_lock:
        series_views[series_uid] = view
        ct_files_cache[series_uid] = view.files
    return view


def get_series_ct_files(series_uid: str) -> List[str]:
    """CT image files of a series sorted by Z position (cached)."""
    with results_cache_lock:
        if series_uid in ct_files_cache:
            return ct_files_cache[series_uid]
    return get_series_view(series_uid).files


def invalidate_viewer_caches(series_uid: str) -> None:
    with results_cache_lock:
        ct_files_cache.pop(series_uid, None)
        series_views.pop(series_uid, None)
    slice_cache.invalidate(series_uid)


def cleanup_old_directories(now: Optional[datetime] = None):
    """Remove stale data at startup.

    STORAGE_DIR: series folders older than ``storage.retention_days`` (0 = never).
    EXPORT_DIR:  exports older than 24 hours.

    Only folders named like a DICOM UID are touched, so pointing STORAGE_DIR
    at a shared location cannot delete unrelated data.
    """
    now = now or datetime.now()

    if settings.STORAGE_RETENTION_DAYS > 0:
        oldest_kept = now.date() - timedelta(days=settings.STORAGE_RETENTION_DAYS - 1)
        _remove_uid_dirs(settings.STORAGE_DIR, lambda mtime: mtime.date() < oldest_kept, "storage")

    one_day_ago = now - timedelta(days=1)
    _remove_uid_dirs(settings.EXPORT_DIR, lambda mtime: mtime < one_day_ago, "TPS_EXPORT")


def _remove_uid_dirs(base_dir: str, is_stale, label: str):
    if not os.path.isdir(base_dir):
        return
    for entry in os.listdir(base_dir):
        if not is_valid_uid(entry):
            continue
        path = os.path.join(base_dir, entry)
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
        except (OSError, ValueError, OverflowError):
            continue
        if is_stale(mtime):
            print(f"Cleanup: removing old series {entry} from {label} (modified {mtime})")
            shutil.rmtree(path, ignore_errors=True)


def load_persisted_results():
    """Load previously saved QA results from disk into results_cache."""
    print("Loading persisted QA results from disk...")
    for entry in list_series_dirs():
        study_path = storage_path(entry)
        cache_file = os.path.join(study_path, "qa_result.json")
        if not os.path.exists(cache_file):
            continue
        try:
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (UnicodeDecodeError, TypeError):
                # Legacy caches written on a different OS platform
                with open(cache_file, "r", encoding="cp1252") as f:
                    data = json.load(f)

            qa_res = QAResult(**data)  # normalises legacy status values
            with results_cache_lock:
                results_cache[entry] = qa_res
            print(f"Loaded cached result for {entry}")

            mtime_ts = None
            try:
                mtime_ts = datetime.fromtimestamp(os.path.getmtime(cache_file)).isoformat()
            except Exception:
                pass
            patient_id = read_patient_id(glob.glob(os.path.join(study_path, "*.dcm")))
            log_qa_result(qa_res, patient_id=patient_id, timestamp=mtime_ts)
        except Exception as e:
            print(f"Error loading cached result for {entry}: {e}")
