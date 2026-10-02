# Agent Instructions & Project Knowledge

This file provides critical context and instructions for AI agents working on the RapidCTQA codebase.

## Core Directives

### 1. Environment & Imports
- **Imports / tests**: Run `pytest` from the repository root or `backend/`; `pyproject.toml` adds the repository root to the import path. For ad-hoc scripts, set `PYTHONPATH` to the repository root.
- **Never point tests at real data**: The root `conftest.py` redirects storage, logs, exports and reports to a temp directory via `RAPIDCTQA_*` env vars. Keep it that way; this machine may have the clinical DICOM share mounted.
- **`import yaml` placement**: In `backend/main.py`, the `import yaml` statement must remain at the top of the file. Moving it or placing it inside a conditional block can cause `NameError` during configuration loading.

### 2. DICOM & Imaging Domain Knowledge
- **PatientAge (0010,1010)**: Stored as an Age String (VR: AS) in formats like 'nnnY', 'nnnM', 'nnnW', or 'nnnD'. Requires parsing to numeric years for logic checks. The system uses a threshold of 18 years for pediatric vs. adult validation.
- **Hounsfield Units (HU)**:
    - **Body Mask**: Defined as voxels > -500 HU.
    - **Gas/Air**: Defined as voxels < -500 HU within the body mask.
    - **Metal**: Threshold defined in `ctqa.yaml` (default: 3000 HU).
- **Truncation Detection**: Checks whether the filled patient body mask touches the image border ring; lateral tolerances per protocol are in `ctqa.yaml` (`thresholds.geometry`).
- **Thresholds**: Every clinical limit lives in `ctqa.yaml` and is validated by `backend/qa_config.py`; agent logic is in `backend/agents/`. Don't hardcode new limits.
- **Status vocabulary**: Use `backend/status.py` (`QAStatus.ACCEPT` / `CONDITIONAL` / `REJECT` / `INFO` / `SKIPPED`). Don't introduce new status strings. Only `CONDITIONAL` / `REJECT` are actionable; `INFO` must never escalate a verdict.
- **One flag per check**: every agent's `evaluate` returns a flag for each of its checks on every run, with the measured value and the limit in the message.

### 3. Verification & Safety
- **Report Verification**: QA findings for truncation, metal, and gas pockets include specific 1-indexed slice numbers. Always verify these ranges when modifying detection logic.
- **Internal Masking**: The `ImplantAuditor` classifies metal by isolating the patient (largest connected component) and creating a 10mm buffer zone via morphological erosion to exclude surface markers.

## Technical Tips
- **FastAPI Port**: The web dashboard and API run on port 8080 by default.
- **DICOM Listener**: Runs on port 11112.
- **Storage**: Default DICOM storage is `data/rtct` as configured in `webApp.yaml`; sites override it in the git-ignored `webApp.local.yaml`.
- **Security**: UIDs used as paths must go through `backend/security.py` (`series_dir`, `valid_series_uid`). Frontend `POST`s go through `apiPost` (adds the CSRF header), and DICOM-derived text must be passed through `esc()` before `innerHTML`.
- **Formatting**: Use `format_slices` in `backend/agents/base.py` (also exposed as `QAEngine._format_slices`) to convert lists of slice indices into compact, human-readable strings (e.g., 'Slices 1-3, 5').
