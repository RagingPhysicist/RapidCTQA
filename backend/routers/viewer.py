import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime

import numpy as np
import pydicom
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from backend import settings, state
from backend.dicom_sender import send_dicom_series
from backend.models import SegmentationRequest
from backend.security import is_valid_uid, valid_series_uid
from backend.segmentation import SegmentationError
from backend.viewer_cache import SeriesView, _SliceEntry, decode_hu, render_slice_png

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/viewer/{series_uid}/info")
async def viewer_info(series_uid: str = Depends(valid_series_uid)):
    """Slice count, patient info, QA flags and W/L presets for the web viewer."""
    dicom_files = state.get_series_ct_files(series_uid)
    if not dicom_files:
        raise HTTPException(status_code=404, detail="Series not found")

    patient_name = "Unknown"
    protocol = "Unknown"
    result = state.results_cache.get(series_uid)
    if result:
        patient_name = result.patient_name
        protocol = result.protocol

    if patient_name == "Unknown" or protocol == "Unknown":
        for f in dicom_files:
            try:
                ds = pydicom.dcmread(f, stop_before_pixels=True)
                if patient_name == "Unknown":
                    patient_name = str(getattr(ds, 'PatientName', 'Unknown'))
                if protocol == "Unknown":
                    p = str(getattr(ds, 'ProtocolName', 'Unknown'))
                    if p != "Unknown" and p.strip() != "":
                        protocol = p
                if patient_name != "Unknown" and protocol != "Unknown":
                    break
            except Exception:
                continue

    # Screen view: attention findings (and SKIPPED) only; passing checks are in the PDF
    screen = state.engine.screen_view(result) if result else None
    flags = [{"name": f.name, "status": f.status, "message": f.message} for f in screen.flags] if screen else []

    wl_presets = {}
    try:
        with open(os.path.join(settings.ROOT_DIR, "WL.json"), "r", encoding="utf-8") as f:
            wl_presets = json.load(f).get("ct_window_level_presets", {})
    except Exception:
        pass

    has_rtss = False
    reference_point = None
    ref_point_slice_idx = None
    if result:
        has_rtss = result.metrics.get("has_rtss", False)
        reference_point = result.metrics.get("reference_point")
        if reference_point:
            # Closest slice to the reference point Z
            ref_z = reference_point['z']
            min_dist = float('inf')
            for i, f in enumerate(dicom_files):
                try:
                    ds = pydicom.dcmread(f, stop_before_pixels=True)
                    dist = abs(float(ds.ImagePositionPatient[2]) - ref_z)
                    if dist < min_dist:
                        min_dist = dist
                        ref_point_slice_idx = i
                    if dist > min_dist and i > 0:
                        break  # moving away from the point
                except Exception:
                    continue

    return {
        "series_uid": series_uid,
        "patient_name": patient_name,
        "protocol": protocol,
        "slice_count": len(dicom_files),
        "flags": flags,
        "passed_checks": screen.passed_checks if screen else 0,
        "show_passed_summary": screen.show_passed_summary if screen else True,
        "wl_presets": wl_presets,
        "has_rtss": has_rtss,
        "reference_point": reference_point,
        "ref_point_slice_idx": ref_point_slice_idx,
    }


def _render_slice_png(
    dcm_path: str,
    window_width: float,
    window_level: float,
    metal_threshold: float,
    reference_point: dict = None,
    show_mask: bool = False,
    slice_mask: np.ndarray = None,
) -> bytes:
    """Render one DICOM file directly (no caches). Kept for tools and tests;
    the viewer endpoint uses the cached path below."""
    view = SeriesView.from_headers([(dcm_path, pydicom.dcmread(dcm_path, stop_before_pixels=True))])
    entry = _SliceEntry(decode_hu(dcm_path))
    return render_slice_png(entry, window_width, window_level, metal_threshold,
                            view.crosshair(0, reference_point), show_mask, slice_mask)


def _slice_etag(series_uid, index, ww, wl, metal_threshold, mask, mask_version, file_path, reference_point) -> str:
    try:
        file_mtime = os.stat(file_path).st_mtime
    except OSError:
        file_mtime = None
    key = repr((series_uid, index, ww, wl, metal_threshold, mask, mask_version, file_mtime, reference_point))
    return '"' + hashlib.sha1(key.encode()).hexdigest()[:20] + '"'


def _render_job(series_uid, index, view, ww, wl, metal_threshold, reference_point, mask, timings):
    t0 = time.perf_counter()
    entry = state.slice_cache.get(series_uid, index, view.files[index])
    t1 = time.perf_counter()
    slice_mask = None
    if mask:
        try:
            slice_mask = state.segmentation_service.get_mask_slice(
                series_uid, index, expected_shape=view.geometry.shape,
                slice_geometry=view.geometry.slices[index])
        except Exception as e:
            print(f"Error loading TotalSegmentator slice mask: {e}")
    t2 = time.perf_counter()
    png = render_slice_png(entry, ww, wl, metal_threshold, view.crosshair(index, reference_point), mask, slice_mask)
    t3 = time.perf_counter()
    timings.update(hu=t1 - t0, mask=t2 - t1, render=t3 - t2)
    return png


@router.get("/viewer/{series_uid}/slice/{index}")
async def viewer_slice(
    request: Request,
    index: int,
    series_uid: str = Depends(valid_series_uid),
    ww: float = Query(default=400.0),
    wl: float = Query(default=40.0),
    metal: bool = Query(default=True),
    mask: bool = Query(default=False),
):
    """Single DICOM slice as a PNG image with W/L and metal overlay applied.

    Warm requests read no DICOM headers and no NIfTI: the file list and
    geometry, decoded HU slices and the DICOM-space body mask are cached.
    Responses carry an ETag and may be cached by the browser for an hour.
    """
    t_start = time.perf_counter()
    view = await asyncio.get_running_loop().run_in_executor(state.viewer_pool, state.get_series_view, series_uid)
    if not view.files:
        raise HTTPException(status_code=404, detail="Series not found")
    if index < 0 or index >= len(view.files):
        raise HTTPException(status_code=400, detail=f"Slice index out of range (0–{len(view.files)-1})")

    metal_threshold = state.engine.thresholds.implants.metal_threshold_hu if metal else 1e9
    result = state.results_cache.get(series_uid)
    reference_point = result.metrics.get("reference_point") if result else None
    mask_version = state.segmentation_service.mask_version(series_uid) if mask else None

    etag = _slice_etag(series_uid, index, ww, wl, metal_threshold, mask, mask_version, view.files[index], reference_point)
    headers = {"Cache-Control": "private, max-age=3600", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    timings: dict = {}
    try:
        png_bytes = await asyncio.get_running_loop().run_in_executor(
            state.viewer_pool, _render_job,
            series_uid, index, view, ww, wl, metal_threshold, reference_point, mask, timings,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Slice render failed: {e}")

    timings["total"] = time.perf_counter() - t_start
    headers["Server-Timing"] = ", ".join(f"{k};dur={v * 1000:.1f}" for k, v in timings.items())
    logger.debug("slice %s[%d] mask=%s %s", series_uid, index, mask, headers["Server-Timing"])
    return Response(content=png_bytes, media_type="image/png", headers=headers)


def _require_segmentation():
    if not state.segmentation_service.is_available:
        raise HTTPException(
            status_code=503,
            detail="TotalSegmentator is not installed. Run: pip install TotalSegmentator torch",
        )


@router.post("/viewer/{series_uid}/segment")
async def segment_series(
    req: SegmentationRequest,
    background_tasks: BackgroundTasks,
    series_uid: str = Depends(valid_series_uid),
):
    """
    Trigger TotalSegmentator body segmentation for a single CT series.
    For a 4DCT acquisition, call this with the reference_phase_uid.
    """
    if not os.path.isdir(state.storage_path(series_uid)):
        raise HTTPException(status_code=404, detail="Series not found")
    _require_segmentation()

    def _run_segmentation():
        try:
            result = state.segmentation_service.run_body_segmentation(
                series_uid=series_uid,
                task=req.task,
                device=req.device,
                fast=req.fast,
                force=req.force,
                on_progress=lambda msg: print(f"[Segmentation {series_uid}] {msg}"),
            )
            with state.results_cache_lock:
                if series_uid in state.results_cache:
                    state.results_cache[series_uid].metrics["segmentation"] = result.to_dict()
            print(f"Segmentation complete for {series_uid}: {len(result.mask_files)} masks")
        except SegmentationError as exc:
            print(f"Segmentation failed for {series_uid}: {exc}")
        except Exception as exc:
            print(f"Unexpected segmentation error for {series_uid}: {exc}")

    background_tasks.add_task(_run_segmentation)
    return {
        "message": f"Segmentation (task={req.task}) started for series {series_uid}",
        "series_uid": series_uid,
        "task": req.task,
    }


@router.post("/studies/group/{group_id:path}/segment")
async def segment_group(group_id: str, req: SegmentationRequest, background_tasks: BackgroundTasks):
    """
    Trigger TotalSegmentator body segmentation for a 4DCT group, once, on the
    group's reference phase (not on every temporal phase).
    """
    with state.fourdct_cache_lock:
        group = state.fourdct_cache.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="4DCT group not found. Refresh the study list first.")

    ref_uid = group.reference_phase_uid
    if not ref_uid:
        raise HTTPException(status_code=422, detail="No reference phase defined for this group.")
    if not is_valid_uid(ref_uid) or not os.path.isdir(state.storage_path(ref_uid)):
        raise HTTPException(status_code=404, detail=f"Reference phase directory not found: {ref_uid}")
    _require_segmentation()

    def _run_group_segmentation():
        try:
            result = state.segmentation_service.run_body_segmentation(
                series_uid=ref_uid,
                task=req.task,
                device=req.device,
                fast=req.fast,
                force=req.force,
                on_progress=lambda msg: print(f"[Segmentation group={group_id}] {msg}"),
            )
            with state.results_cache_lock:
                if ref_uid in state.results_cache:
                    state.results_cache[ref_uid].metrics["segmentation"] = result.to_dict()
                    state.results_cache[ref_uid].metrics["segmentation_is_4dct_reference"] = True
            print(f"Group segmentation complete for {group_id} (reference phase: {ref_uid}): {len(result.mask_files)} masks")
        except SegmentationError as exc:
            print(f"Group segmentation failed for {group_id}: {exc}")
        except Exception as exc:
            print(f"Unexpected group segmentation error for {group_id}: {exc}")

    background_tasks.add_task(_run_group_segmentation)
    return {
        "message": f"Segmentation (task={req.task}) started for 4DCT group",
        "group_id": group_id,
        "reference_phase_uid": ref_uid,
        "task": req.task,
    }


@router.get("/viewer/{series_uid}/segmentation")
async def get_segmentation_status(series_uid: str = Depends(valid_series_uid)):
    """Segmentation metadata for a series if available."""
    study_path = state.storage_path(series_uid)
    if not os.path.isdir(study_path):
        raise HTTPException(status_code=404, detail="Series not found")

    with state.results_cache_lock:
        cached = state.results_cache.get(series_uid)
        if cached and "segmentation" in cached.metrics:
            return {"available": True, "segmentation": cached.metrics["segmentation"]}

    base_seg_dir = os.path.join(study_path, "segmentations")
    if os.path.isdir(base_seg_dir):
        tasks_found = [t for t in os.listdir(base_seg_dir) if os.path.isdir(os.path.join(base_seg_dir, t))]
        if tasks_found:
            return {"available": True, "tasks": tasks_found}

    return {"available": False, "totalsegmentator_installed": state.segmentation_service.is_available}


@router.post("/viewer/{series_uid}/approve")
async def approve_series(series_uid: str = Depends(valid_series_uid)):
    """Approve a series: export to TPS and route via DICOM."""
    study_path = state.storage_path(series_uid)
    if not os.path.isdir(study_path):
        raise HTTPException(status_code=404, detail="Series not found")
    if state.listener.is_ingesting(series_uid):
        raise HTTPException(status_code=409, detail="Series is still being received")
    try:
        dest = state.export_path(series_uid)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(study_path, dest)
        send_dicom_series(study_path)
        return {"message": f"{series_uid} approved and routed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Approval failed: {e}")


@router.post("/viewer/{series_uid}/reject")
async def reject_series(series_uid: str = Depends(valid_series_uid)):
    """Reject a series, delete all its data, and log the decision."""
    try:
        study_path = state.storage_path(series_uid)
        if os.path.isdir(study_path):
            shutil.rmtree(study_path, ignore_errors=True)

        export_path = state.export_path(series_uid)
        if os.path.isdir(export_path):
            shutil.rmtree(export_path, ignore_errors=True)

        pdf_path = state.report_path(series_uid)
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception:
                pass

        with state.results_cache_lock:
            state.results_cache.pop(series_uid, None)
        state.invalidate_viewer_caches(series_uid)

        with open(os.path.join(settings.ROOT_DIR, "rejections.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} - {series_uid} rejected and deleted\n")

        return {"message": f"{series_uid} rejected"}
    except Exception as e:
        print(f"Error during series rejection: {e}")
        raise HTTPException(status_code=500, detail=f"Rejection failed: {e}")
