# Development & Testing Guide

## Project Structure
- `backend/`: Core Python application logic.
  - `main.py`: FastAPI app: middleware, routers, startup.
  - `routers/`: API endpoints (`studies.py`, `viewer.py`, `reports.py`).
  - `state.py`: Shared runtime state (QA engine, caches, listener) and the analysis pipeline.
  - `settings.py`: Loads `webApp.yaml` + `webApp.local.yaml` + environment overrides.
  - `security.py`: UID / path validation, client allow-list and CSRF guard.
  - `engine.py`: QA engine orchestration (reads series, builds masks, runs agents).
  - `agents/`: One module per QA agent (`compute` metrics, `evaluate` flags).
  - `qa_config.py`: Typed, validated loader for `ctqa.yaml`.
  - `status.py`: The single status vocabulary (`ACCEPT` / `CONDITIONAL` / `REJECT` ...).
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
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime + test dependencies
```

### 2. Site configuration
Copy `webApp.local.example.yaml` to `webApp.local.yaml` and set your storage share and network ranges (see [CONFIGURATION.md](CONFIGURATION.md)). Without it the app stores data in `data/rtct` and only serves `127.0.0.1`.

## Running the Application
To start the backend and DICOM listener:
```bash
python run.py
```

## Testing
The project uses `pytest` for testing.

### Running Backend Tests
From the repository root (or `backend/`):
```bash
pytest
```
`pyproject.toml` puts the repository root on the import path, so no `PYTHONPATH` setup is needed. The root `conftest.py` points storage, logs, exports and reports at a temporary directory, so the suite never touches a real DICOM share or the clinical logs, whatever `webApp.local.yaml` says.

### Key Test Files
- `backend/test_implant_auditor.py`: Tests for metal detection logic.
- `backend/test_dicom_sender.py`: Tests for DICOM networking/egress.
- `backend/test_refined_pca.py`: Tests for advanced geometry/alignment logic.
- `backend/test_hardening.py`: Config validation, status normalisation, UID/path checks, listener and cleanup behaviour.
- `backend/test_rejection.py`: Reject endpoint, CSRF and client allow-list.

## Concurrency & Resource Optimization
The application enforces strict limits on concurrency to prevent host CPU and RAM exhaustion:
- **API Worker Pool**: Defined in `backend/main.py` using `ThreadPoolExecutor(max_workers=4)` to throttle the concurrent analysis of different series.
- **DICOM I/O Pool**: Reading files parallelizes via `ThreadPoolExecutor(max_workers=4)` inside the engine to optimize sequential disk reads without queue thrashing.
- **CPU Slice Processing**: Python threads are bound by the Global Interpreter Lock (GIL). Running too many concurrent threads for heavy math operations like morphological erosion degrades performance. Slice processing inside the engine uses `ThreadPoolExecutor(max_workers=2)` to prevent context-shifting overhead.

## QA Results Cache & Disk Persistence
To prevent duplicate analysis on application restarts:
- Completed `QAResult` objects are written to disk as `qa_result.json` directly within the series storage directory (`STORAGE_DIR/{series_uid}/qa_result.json`). Legacy status values in older files are normalised on load.
- Upon starting up, the API loads all existing `qa_result.json` files from `STORAGE_DIR` into `results_cache` in memory.
- Scans that have already been evaluated skip re-analysis and are not re-sent to DICOM destinations.

## Contribution Guidelines
- **Always update documentation**: If you change agent logic or API endpoints, update the corresponding files in `docs/`.
- **Verify with Cockpit**: Use `cockpit.py` to visually verify any changes to image processing logic.
- **Maintain `import yaml` placement**: In `backend/main.py`, ensure `import yaml` remains at the top level to avoid scope issues during configuration loading.
- **No import-time side effects**: Directory creation, cleanup and the DICOM listener start in the FastAPI lifespan (`backend/main.py`), never at import, so tests and tools can import the app safely.
