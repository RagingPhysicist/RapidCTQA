# Agent Instructions & Project Knowledge

This file provides critical context and instructions for AI agents working on the RapidCTQA codebase.

## Core Directives

### 1. Environment & Imports
- **PYTHONPATH**: When running backend scripts or tests (e.g., in the `backend/` directory), you **must** set the `PYTHONPATH` to include the repository root to resolve internal module imports.
- **`import yaml` placement**: In `backend/main.py`, the `import yaml` statement must remain at the top of the file. Moving it or placing it inside a conditional block can cause `NameError` during configuration loading.

### 2. DICOM & Imaging Domain Knowledge
- **PatientAge (0010,1010)**: Stored as an Age String (VR: AS) in formats like 'nnnY', 'nnnM', 'nnnW', or 'nnnD'. Requires parsing to numeric years for logic checks. The system uses a threshold of 18 years for pediatric vs. adult validation.
- **Hounsfield Units (HU)**:
    - **Body Mask**: Defined as voxels > -500 HU.
    - **Gas/Air**: Defined as voxels < -500 HU within the body mask.
    - **Metal**: Threshold defined in `ctqa.yaml` (default: 3000 HU).
- **Truncation Detection**: Scans the image matrix perimeter for pixels exceeding a skin threshold (default: -200 HU).

### 3. Verification & Safety
- **Report Verification**: QA findings for truncation, metal, and gas pockets include specific 1-indexed slice numbers. Always verify these ranges when modifying detection logic.
- **Internal Masking**: The `ImplantAuditor` classifies metal by isolating the patient (largest connected component) and creating a 10mm buffer zone via morphological erosion to exclude surface markers.

## Technical Tips
- **FastAPI Port**: The web dashboard and API run on port 8080 by default.
- **DICOM Listener**: Runs on port 11112.
- **Storage**: Default DICOM storage is `data/rtct` as configured in `webApp.yaml`.
- **Formatting**: Use the `_format_slices` helper method in `QAEngine` to convert lists of slice indices into compact, human-readable strings (e.g., 'Slices 1-3, 5').
