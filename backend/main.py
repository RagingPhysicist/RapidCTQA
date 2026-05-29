

# Load webApp configuration
with open("webApp.yaml", "r") as f:
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

STORAGE_DIR = normalise_storage_path(config_web.get("backend", {}).get("storage", {}).get("path", "./data/rtct"))
EXPORT_DIR = "./TPS_EXPORT"
REPORTS_DIR = "./reports"
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
print(f"Using storage directory: {STORAGE_DIR}")
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

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
        
        # Auto-export if ACCEPTED
        if result.status == "ACCEPT":
            dest = os.path.join(EXPORT_DIR, series_uid)
            if not os.path.exists(dest):
                print(f"Auto-exporting {series_uid} to TPS...")
                shutil.copytree(study_path, dest)

listener = DicomListener(STORAGE_DIR, on_series_received)

@app.on_event("startup")
async def startup_event():
    dl_config = config_web.get("backend", {}).get("dicom_listener", {})
    host = dl_config.get("host", "0.0.0.0")
    port = dl_config.get("port", 11112)
    listener.start(host=host, port=port)

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
    uvicorn.run(app, host="127.0.0.1", port=8080)
