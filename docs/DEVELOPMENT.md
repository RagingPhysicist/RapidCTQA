# Development & Testing Guide

## Project Structure
- `backend/`: Core Python application logic.
  - `main.py`: FastAPI application and API routes.
  - `engine.py`: The QA Engine containing all agent logic.
  - `listener.py`: DICOM SCP listener implementation.
  - `reporter.py`: PDF report generation logic.
  - `models.py`: Pydantic models for API data structures.
- `frontend/`: Static web assets (HTML, CSS, JS).
- `docs/`: Technical documentation.
- `data/rtct/`: Default directory for storing received DICOM series (automatically created).
- `reports/`: Generated PDF reports.
- `TPS_EXPORT/`: Directory for series that passed QA and are ready for export.

## Development Setup

### 1. Environment
It is recommended to use a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Variables
To ensure internal module resolution, add the repository root to your `PYTHONPATH`:
```bash
# On Linux/macOS
export PYTHONPATH=$PYTHONPATH:.
# On Windows (PowerShell)
$env:PYTHONPATH += ";."
```

## Running the Application
To start the backend and DICOM listener:
```bash
python run.py
```

## Testing
The project uses `pytest` for testing.

### Running Backend Tests
Ensure your `PYTHONPATH` is set correctly, then run:
```bash
pytest backend/
```

### Key Test Files
- `backend/test_implant_auditor.py`: Tests for metal detection logic.
- `backend/test_dicom_sender.py`: Tests for DICOM networking/egress.
- `backend/test_refined_pca.py`: Tests for advanced geometry/alignment logic.

## Concurrency & Resource Optimization
The application enforces strict limits on concurrency to prevent host CPU and RAM exhaustion:
- **API Worker Pool**: Defined in `backend/main.py` using `ThreadPoolExecutor(max_workers=4)` to throttle the concurrent analysis of different series.
- **DICOM I/O Pool**: Reading files parallelizes via `ThreadPoolExecutor(max_workers=4)` inside the engine to optimize sequential disk reads without queue thrashing.
- **CPU Slice Processing**: Python threads are bound by the Global Interpreter Lock (GIL). Running too many concurrent threads for heavy math operations like morphological erosion degrades performance. Slice processing inside the engine uses `ThreadPoolExecutor(max_workers=2)` to prevent context-shifting overhead.

## QA Results Cache & Disk Persistence
To prevent duplicate analysis on application restarts:
- Completed `QAResult` objects are written to disk as `qa_result.json` directly within the series storage directory (`STORAGE_DIR/{series_uid}/qa_result.json`).
- Upon starting up, the API loads all existing `qa_result.json` files from `STORAGE_DIR` into `results_cache` in memory.
- Scans that have already been evaluated skip re-analysis and are not re-sent to DICOM destinations.

## Contribution Guidelines
- **Always update documentation**: If you change agent logic or API endpoints, update the corresponding files in `docs/`.
- **Verify with Cockpit**: Use `cockpit.py` to visually verify any changes to image processing logic.
- **Maintain `import yaml` placement**: In `backend/main.py`, ensure `import yaml` remains at the top level to avoid scope issues during configuration loading.
