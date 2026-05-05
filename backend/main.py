from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import os
import glob
import pydicom
import subprocess
import sys
import pydicom
from typing import List, Dict
from .models import QAResult, StudySummary, IngestionStatus
from .engine import QAEngine
from .listener import DicomListener

app = FastAPI(title="RapidCTQA API")

# Enable CORS for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = "./data/rtct"
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
os.makedirs(STORAGE_DIR, exist_ok=True)

engine = QAEngine("ctqa.yaml")
results_cache: Dict[str, QAResult] = {}

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
        results_cache[series_uid] = result
        print(f"Analysis complete for {series_uid}: {result.status}")

listener = DicomListener(STORAGE_DIR, on_series_received)

@app.on_event("startup")
async def startup_event():
    listener.start()

@app.get("/api/status", response_model=IngestionStatus)
async def get_status():
    return IngestionStatus(
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
            
        # Get metadata from first file
        ds = pydicom.dcmread(files[0], stop_before_pixels=True)
        patient_name = str(getattr(ds, 'PatientName', 'Unknown'))
        protocol = str(getattr(ds, 'ProtocolName', 'Unknown'))
        patient_id = str(getattr(ds, 'PatientID', 'Unknown'))
        study_date = str(getattr(ds, 'StudyDate', 'Unknown'))
        
        status = "PENDING"
        if series_uid in results_cache:
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

@app.post("/api/launch_cockpit/{series_uid}")
async def launch_cockpit(series_uid: str):
    try:
        # Launch cockpit.py with the current python executable
        subprocess.Popen([sys.executable, "cockpit.py", series_uid])
        return {"message": f"Cockpit launched for {series_uid}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to launch cockpit: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
