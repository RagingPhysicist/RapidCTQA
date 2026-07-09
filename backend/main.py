from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import asyncio
from datetime import datetime, timedelta
import glob
import pydicom
import io
import shutil
import threading
import yaml
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict
import numpy as np
from PIL import Image
try:
    from .models import QAResult, StudySummary, IngestionStatus
    from .engine import QAEngine
    from .listener import DicomListener
    from .reporter import generate_pdf_report
    from .dicom_sender import send_dicom_series
except ImportError:
    import sys
    # Add root folder to sys.path when running main.py directly
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.models import QAResult, StudySummary, IngestionStatus
    from backend.engine import QAEngine
    from backend.listener import DicomListener
    from backend.reporter import generate_pdf_report
    from backend.dicom_sender import send_dicom_series

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load webApp configuration
with open(os.path.join(ROOT_DIR, "webApp.yaml"), "r") as f:
    config_web = yaml.safe_load(f)

app = FastAPI(title="RapidCTQA API")

# Enable CORS for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def normalise_storage_path(path: str) -> str:
    """
    Normalise the storage path, falling back to mapped drive letter on Windows if needed.
    """
    if not path:
        return "./data/rtct"
    if os.name == 'nt':
        norm = os.path.normpath(path)
        unc_prefix = "\\\\imgserver\\DICOM"
        if norm.startswith(unc_prefix):
            try:
                # If network path is accessible directly, use it
                if os.path.exists(unc_prefix):
                    return norm
            except Exception:
                pass
            
            # Try S: drive fallback if UNC path is not accessible but S: exists
            s_fallback = norm.replace(unc_prefix, "S:")
            try:
                if os.path.exists("S:\\") or os.path.exists("S:"):
                    return s_fallback
            except Exception:
                pass
    return path

raw_storage_path = config_web.get("backend", {}).get("storage", {}).get("path", "")
if not raw_storage_path:
    raw_storage_path = os.path.join(ROOT_DIR, "data", "rtct")
elif not os.path.isabs(raw_storage_path):
    raw_storage_path = os.path.join(ROOT_DIR, raw_storage_path)

STORAGE_DIR = normalise_storage_path(raw_storage_path)
EXPORT_DIR = os.path.join(ROOT_DIR, "TPS_EXPORT")
REPORTS_DIR = os.path.join(ROOT_DIR, "reports")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
print(f"Using storage directory: {STORAGE_DIR}")
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)


def _cleanup_old_directories():
    """Remove stale data at startup.

    STORAGE_DIR: keep only series received today.
    EXPORT_DIR:  keep only exports less than 24 hours old.
    """
    today = datetime.now().date()
    one_day_ago = datetime.now() - timedelta(days=1)

    # --- STORAGE_DIR: keep today only ---
    for entry in os.listdir(STORAGE_DIR):
        path = os.path.join(STORAGE_DIR, entry)
        if not os.path.isdir(path):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
        except (OSError, ValueError, OverflowError):
            continue
        if mtime.date() < today:
            print(f"Cleanup: removing old series {entry} from storage (modified {mtime.date()})")
            shutil.rmtree(path, ignore_errors=True)

    # --- EXPORT_DIR: keep last 24 h ---
    for entry in os.listdir(EXPORT_DIR):
        path = os.path.join(EXPORT_DIR, entry)
        if not os.path.isdir(path):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
        except (OSError, ValueError, OverflowError):
            continue
        if mtime < one_day_ago:
            print(f"Cleanup: removing old export {entry} from TPS_EXPORT (modified {mtime})")
            shutil.rmtree(path, ignore_errors=True)


_cleanup_old_directories()

engine = QAEngine(os.path.join(ROOT_DIR, "ctqa.yaml"))
results_cache: Dict[str, QAResult] = {}
ct_files_cache: Dict[str, List[str]] = {}
results_cache_lock = threading.Lock()

# Pool for concurrent series analysis (IO + CPU-bound work per series)
analysis_pool = ThreadPoolExecutor(max_workers=4)

# Serve frontend
@app.get("/")
async def read_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

def on_series_received(series_uid: str):
    print(f"Received series: {series_uid}. Starting analysis...")
    study_path = os.path.join(STORAGE_DIR, series_uid)
    dicom_files = glob.glob(os.path.join(study_path, "*.dcm"))
    if dicom_files:
        result = engine.analyze_series(dicom_files)
        with results_cache_lock:
            results_cache[series_uid] = result
        print(f"Analysis complete for {series_uid}: {result.status}")
        
        # Save to disk
        cache_file = os.path.join(study_path, "qa_result.json")
        try:
            with open(cache_file, "w") as f:
                f.write(result.json())
            print(f"Cached result saved to disk: {cache_file}")
        except Exception as e:
            print(f"Error saving cached result to disk: {e}")
        
        # Generate PDF report automatically in reports folder
        pdf_path = os.path.join(REPORTS_DIR, f"QA_Report_{series_uid}.pdf")
        try:
            generate_pdf_report(result, pdf_path)
            print(f"PDF report generated automatically: {pdf_path}")
        except Exception as e:
            print(f"Error auto-generating PDF report: {e}")
        
        # Auto-export if ACCEPTED
        if result.status == "ACCEPT":
            dest = os.path.join(EXPORT_DIR, series_uid)
            if not os.path.exists(dest):
                print(f"Auto-exporting {series_uid} to TPS...")
                shutil.copytree(study_path, dest, dirs_exist_ok=True)
            
            print(f"Auto-routing accepted series {series_uid} to DICOM destinations...")
            send_dicom_series(study_path)

def _submit_series(series_uid: str):
    """Submit a series for analysis on the shared thread pool."""
    analysis_pool.submit(on_series_received, series_uid)

listener = DicomListener(STORAGE_DIR, _submit_series)

def _load_persisted_results():
    """Load previously saved QA results from disk to results_cache."""
    print("Loading persisted QA results from disk...")
    for entry in os.listdir(STORAGE_DIR):
        study_path = os.path.join(STORAGE_DIR, entry)
        if not os.path.isdir(study_path):
            continue
        cache_file = os.path.join(study_path, "qa_result.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    data = json.load(f)
                with results_cache_lock:
                    results_cache[entry] = QAResult(**data)
                print(f"Loaded cached result for {entry}")
            except Exception as e:
                print(f"Error loading cached result for {entry}: {e}")

@app.on_event("startup")
async def startup_event():
    _load_persisted_results()
    dl_config = config_web.get("backend", {}).get("dicom_listener", {})
    host = dl_config.get("host", "0.0.0.0")
    port = dl_config.get("port", 11112)
    listener.start(host=host, port=port)

@app.get("/api/status", response_model=IngestionStatus)
async def get_status():
    return IngestionStatus(
        version=str(config_web.get("app", {}).get("version", "0.0")),
        active_transfers=0,
        queue_size=len(results_cache),
        processed_today=len(results_cache)
    )

@app.get("/api/studies", response_model=List[StudySummary])
async def get_studies(background_tasks: BackgroundTasks):
    summaries = []
    for series_uid in os.listdir(STORAGE_DIR):
        study_path = os.path.join(STORAGE_DIR, series_uid)
        if not os.path.isdir(study_path):
            continue
            
        files = glob.glob(os.path.join(study_path, "*.dcm"))
        if not files:
            continue
            
        # Use cached result if available for core fields
        cached_res = results_cache.get(series_uid)

        patient_name = "Unknown"
        protocol = "Unknown"
        patient_id = "Unknown"
        study_date = "Unknown"

        if cached_res:
            patient_name = cached_res.patient_name
            protocol = cached_res.protocol

        # Find first CT file for remaining metadata if needed
        metadata_found = False
        if not cached_res or protocol == "Unknown" or patient_id == "Unknown":
            # Sort files to be somewhat deterministic, but try to find a CT slice
            for f in sorted(files):
                try:
                    ds = pydicom.dcmread(f, stop_before_pixels=True)
                    # Prefer CT Image Storage
                    if getattr(ds, 'SOPClassUID', '') == '1.2.840.10008.5.1.4.1.1.2':
                        patient_name = str(getattr(ds, 'PatientName', patient_name))
                        protocol = str(getattr(ds, 'ProtocolName', protocol))
                        patient_id = str(getattr(ds, 'PatientID', 'Unknown'))
                        study_date = str(getattr(ds, 'StudyDate', 'Unknown'))
                        metadata_found = True
                        break
                except Exception:
                    continue

            # Fallback to first file if no CT found
            if not metadata_found:
                ds = pydicom.dcmread(files[0], stop_before_pixels=True)
                patient_name = str(getattr(ds, 'PatientName', patient_name))
                protocol = str(getattr(ds, 'ProtocolName', protocol))
                patient_id = str(getattr(ds, 'PatientID', 'Unknown'))
                study_date = str(getattr(ds, 'StudyDate', 'Unknown'))
        
        status = "PENDING"
        if listener.is_ingesting(series_uid):
            status = "INGESTING"
        elif series_uid in results_cache:
            status = results_cache[series_uid].status
        else:
            # Trigger analysis in background if not already cached
            background_tasks.add_task(on_series_received, series_uid)
        
        summaries.append(StudySummary(
            series_uid=series_uid,
            patient_name=patient_name,
            patient_id=patient_id,
            protocol=protocol,
            study_date=study_date,
            modality="CT",
            status=status,
            instance_count=len(files)
        ))
    status_priority = {
        "REJECT": 0,
        "CONDITIONAL": 1,
        "ACCEPT": 2,
        "PASS": 2,
        "PENDING": 3,
        "INGESTING": 4
    }
    summaries.sort(key=lambda s: status_priority.get(s.status.upper(), 99))
    return summaries

@app.get("/api/studies/{series_uid}", response_model=QAResult)
async def get_study_detail(series_uid: str):
    if series_uid not in results_cache:
        # Try to run validation if files exist but result not cached
        study_path = os.path.join(STORAGE_DIR, series_uid)
        dicom_files = glob.glob(os.path.join(study_path, "*.dcm"))
        if dicom_files:
            on_series_received(series_uid)
            
    if series_uid in results_cache:
        return results_cache[series_uid]
    
    raise HTTPException(status_code=404, detail="Study not found or not yet processed")

@app.post("/api/validate/{series_uid}")
async def run_validation(series_uid: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(on_series_received, series_uid)
    return {"message": "Validation triggered"}

def _get_series_ct_files(series_uid: str) -> List[str]:
    """Get only CT image files for a series, sorted by Z-position. Uses caching."""
    with results_cache_lock:
        if series_uid in ct_files_cache:
            return ct_files_cache[series_uid]

    study_path = os.path.join(STORAGE_DIR, series_uid)
    all_files = glob.glob(os.path.join(study_path, "*.dcm"))
    ct_files = []
    for f in all_files:
        try:
            # Quick check SOP Class without full read if possible
            ds = pydicom.dcmread(f, stop_before_pixels=True)
            if ds.SOPClassUID == '1.2.840.10008.5.1.4.1.1.2':
                ct_files.append((f, float(getattr(ds, 'ImagePositionPatient', [0, 0, 0])[2])))
        except Exception:
            continue

    # Sort by Z
    ct_files.sort(key=lambda x: x[1])
    sorted_paths = [f[0] for f in ct_files]

    with results_cache_lock:
        ct_files_cache[series_uid] = sorted_paths

    return sorted_paths


@app.get("/api/viewer/{series_uid}/info")
async def viewer_info(series_uid: str):
    """Return slice count, sorted file list index, patient info and QA flags for the web viewer."""
    dicom_files = _get_series_ct_files(series_uid)
    if not dicom_files:
        raise HTTPException(status_code=404, detail="Series not found")

    patient_name = "Unknown"
    protocol = "Unknown"

    # Try to get from cache first
    if series_uid in results_cache:
        res = results_cache[series_uid]
        patient_name = res.patient_name
        protocol = res.protocol

    # Fallback to robust reading from files if cache is missing or incomplete
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

    flags = []
    if series_uid in results_cache:
        result = results_cache[series_uid]
        flags = [{"name": f.name, "status": f.status, "message": f.message} for f in result.flags]

    # Load WL presets from WL.json
    wl_presets = {}
    wl_path = os.path.join(ROOT_DIR, "WL.json")
    try:
        with open(wl_path) as f:
            wl_presets = json.load(f).get("ct_window_level_presets", {})
    except Exception:
        pass

    # RTSS and Reference Point
    has_rtss = False
    reference_point = None
    if series_uid in results_cache:
        result = results_cache[series_uid]
        has_rtss = result.metrics.get("has_rtss", False)
        reference_point = result.metrics.get("reference_point")

    return {
        "series_uid": series_uid,
        "patient_name": patient_name,
        "protocol": protocol,
        "slice_count": len(dicom_files),
        "flags": flags,
        "wl_presets": wl_presets,
        "has_rtss": has_rtss,
        "reference_point": reference_point,
    }


def _render_slice_png(dcm_path: str, window_width: float, window_level: float, metal_threshold: float, reference_point: dict = None) -> bytes:
    """Render a single DICOM slice as a PNG byte stream with W/L and optional metal overlay."""
    ds = pydicom.dcmread(dcm_path)
    img = ds.pixel_array.astype(np.float32)
    slope = float(getattr(ds, 'RescaleSlope', 1.0))
    intercept = float(getattr(ds, 'RescaleIntercept', 0.0))
    img = img * slope + intercept

    vmin = window_level - window_width / 2
    vmax = window_level + window_width / 2
    img_clipped = np.clip(img, vmin, vmax)
    img_norm = ((img_clipped - vmin) / (vmax - vmin) * 255).astype(np.uint8)

    # Convert to RGB so we can draw a coloured metal overlay
    rgb = np.stack([img_norm, img_norm, img_norm], axis=-1)

    # Metal overlay: pixels above threshold -> red tint
    metal_mask = img > metal_threshold
    if np.any(metal_mask):
        rgb[metal_mask] = np.array([220, 50, 50], dtype=np.uint8)

    # Reference point overlay (crosshair)
    if reference_point:
        try:
            # Check if Z matches
            img_z = float(ds.ImagePositionPatient[2])
            slice_thickness = float(getattr(ds, 'SliceThickness', 2.0))
            if abs(img_z - reference_point['z']) < (slice_thickness / 2.0):
                # Convert patient coordinates to pixel coordinates
                # Pixel = (Patient - Origin) / Spacing
                origin = ds.ImagePositionPatient
                spacing = ds.PixelSpacing # [Row Spacing, Column Spacing]
                px_x = int((reference_point['x'] - float(origin[0])) / float(spacing[1]))
                px_y = int((reference_point['y'] - float(origin[1])) / float(spacing[0]))

                rows, cols = rgb.shape[0], rgb.shape[1]
                if 0 <= px_x < cols and 0 <= px_y < rows:
                    # Draw a yellow crosshair (size 20px)
                    size = 10
                    # Vertical line
                    rgb[max(0, px_y-size):min(rows, px_y+size), px_x] = [255, 255, 0]
                    # Horizontal line
                    rgb[px_y, max(0, px_x-size):min(cols, px_x+size)] = [255, 255, 0]
        except Exception as e:
            print(f"Error drawing reference point: {e}")

    pil_img = Image.fromarray(rgb, mode='RGB')
    buf = io.BytesIO()
    pil_img.save(buf, format='PNG', optimize=False)
    buf.seek(0)
    return buf.read()


@app.get("/api/viewer/{series_uid}/slice/{index}")
async def viewer_slice(
    series_uid: str,
    index: int,
    ww: float = Query(default=400.0),
    wl: float = Query(default=40.0),
    metal: bool = Query(default=True),
):
    """Return a single DICOM slice as a PNG image with W/L and metal overlay applied."""
    dicom_files = _get_series_ct_files(series_uid)
    if not dicom_files:
        raise HTTPException(status_code=404, detail="Series not found")
    if index < 0 or index >= len(dicom_files):
        raise HTTPException(status_code=400, detail=f"Slice index out of range (0–{len(dicom_files)-1})")

    metal_threshold = engine.config.get("thresholds", {}).get("implants", {}).get("metal_threshold_hu", 3000) if metal else 1e9

    reference_point = None
    if series_uid in results_cache:
        reference_point = results_cache[series_uid].metrics.get("reference_point")

    try:
        png_bytes = await asyncio.get_event_loop().run_in_executor(
            analysis_pool,
            _render_slice_png,
            dicom_files[index],
            ww,
            wl,
            metal_threshold,
            reference_point,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Slice render failed: {e}")

    return StreamingResponse(io.BytesIO(png_bytes), media_type="image/png")


@app.post("/api/viewer/{series_uid}/approve")
async def approve_series(series_uid: str):
    """Approve a series: export to TPS and route via DICOM."""
    study_path = os.path.join(STORAGE_DIR, series_uid)
    if not os.path.isdir(study_path):
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        dest = os.path.join(EXPORT_DIR, series_uid)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(study_path, dest)
        send_dicom_series(study_path)
        return {"message": f"{series_uid} approved and routed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Approval failed: {e}")


@app.post("/api/viewer/{series_uid}/reject")
async def reject_series(series_uid: str):
    """Reject a series, delete all its data, and log the decision."""
    try:
        # 1. Remove from STORAGE_DIR
        study_path = os.path.join(STORAGE_DIR, series_uid)
        if os.path.isdir(study_path):
            shutil.rmtree(study_path, ignore_errors=True)

        # 2. Remove from EXPORT_DIR
        export_path = os.path.join(EXPORT_DIR, series_uid)
        if os.path.isdir(export_path):
            shutil.rmtree(export_path, ignore_errors=True)

        # 3. Remove PDF report
        pdf_path = os.path.join(REPORTS_DIR, f"QA_Report_{series_uid}.pdf")
        if os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except Exception:
                pass

        # 4. Clear caches
        with results_cache_lock:
            results_cache.pop(series_uid, None)
            ct_files_cache.pop(series_uid, None)

        # 5. Log rejection
        log_path = os.path.join(ROOT_DIR, "rejections.log")
        with open(log_path, "a") as f:
            f.write(f"{datetime.now().isoformat()} - {series_uid} rejected and deleted\n")

        return {"message": f"{series_uid} rejected"}
    except Exception as e:
        print(f"Error during series rejection: {e}")
        raise HTTPException(status_code=500, detail=f"Rejection failed: {e}")

@app.get("/api/reports/{series_uid}/pdf")
async def get_pdf_report(series_uid: str):
    if series_uid not in results_cache:
        # Try to run validation if files exist but result not cached
        study_path = os.path.join(STORAGE_DIR, series_uid)
        dicom_files = glob.glob(os.path.join(study_path, "*.dcm"))
        if dicom_files:
            on_series_received(series_uid)
        else:
            raise HTTPException(status_code=404, detail="Study not found")

    result = results_cache[series_uid]
    pdf_path = os.path.join(REPORTS_DIR, f"QA_Report_{series_uid}.pdf")
    
    try:
        generate_pdf_report(result, pdf_path)
        return FileResponse(
            pdf_path, 
            media_type="application/pdf", 
            filename=f"RapidCTQA_Report_{result.patient_name}_{series_uid[:8]}.pdf"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF Generation failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
