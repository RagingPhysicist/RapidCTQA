# RapidCTQA

RapidCTQA is a specialized automated Quality Assurance (QA) tool for radiotherapy treatment planning CT datasets. It provides real-time analysis of DICOM series to ensure they meet clinical integrity, geometry, and image quality standards before contouring and planning.

## Features

- **Automated DICOM Ingestion**: Integrated DICOM SCP (C-STORE) listener for seamless ingestion from PACS or CT Scanners.
- **Multi-Agent Analysis**: A suite of specialized agents evaluates geometry, image quality, HU accuracy, and clinical protocols.
- **Artifact & Metal Detection**: Advanced detection of truncation, excessive bowel gas, and metallic implants (internal, surface, and external).
- **Patient Alignment**: Automated detection of patient roll using Radon transform bilateral reflection symmetry profiling on the central slice.
- **Interactive Dashboard**: Modern web interface for reviewing studies, detailed metrics, and QA flags.
- **Automated Reporting**: Generates comprehensive PDF QA reports with slice-indexed findings.
- **Clinical Integration**: Auto-exports accepted series to a designated "TPS Export" directory and routes them to configured DICOM destinations.
- **Cockpit Tool**: Includes a dedicated visualization tool (`cockpit.py`) for detailed manual inspection of flagged series.

## System Architecture

- **Backend**: Python-based FastAPI application orchestrating the QA Engine and API.
- **DICOM Listener**: `pynetdicom`-powered service that receives and buffers incoming DICOM series.
- **QA Engine**: Multi-threaded processing engine that performs voxel-level analysis using `numpy` and `scipy`.
- **Frontend**: Responsive JavaScript/HTML dashboard that communicates with the backend via REST API.
- **Reporting**: Automated PDF generation using `fpdf2`.

## Quick Start

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **Run the Application**:
    ```bash
    python run.py
    ```
    - **Web Dashboard**: `http://localhost:8080`
    - **DICOM Listener**: `0.0.0.0:11112` (AET: Configurable in `webApp.yaml`, defaults to `RT_QA_SCP`)

## QA Agents & Logic

### 1. GeometryGuardian (Geometry & Truncation)
Ensures geometric integrity and FOV coverage.
- **Truncation**: Detects if anatomy touches the FOV edge (3px buffer, > -200 HU).
- **Consistency**: Validates monotonic slice positions and consistent slice spacing.
- **Tilt**: Flags gantry tilt exceeding 1.0°.

### 2. NoiseWhisperer (Image Quality)
Analyzes hardware performance and calibration.
- **Noise**: Measures Standard Deviation in 20x20px background air regions (corners).
- **Air HU**: Estimates Air HU via 1st percentile; flags if outside [-1100, -900] HU.

### 3. FluidPhysicist (HU Accuracy)
Validates CT number consistency using biological markers.
- **HU Consistency**: Evaluates median HU of soft tissue and fluid (0-50 HU range).
- **Rescale Slope**: Ensures valid DICOM rescale metadata.

### 4. CavityScout (Air & Gas Auditor)
Detects gas pockets that may impact dose calculation.
- **Logic**: Isolates voxels < -500 HU within the body mask.
- **Thresholds**: Flags moderate (>15cc) or excessive (>50cc) gas.

### 5. ImplantAuditor (Metal Detection)
Detects and classifies metallic objects (>2000 HU).
- **Classification**: Distinguishes between internal implants, surface markers, and external objects.
- **Validation**: Uses morphological erosion to define an internal body buffer.

### 6. AlignmentAuditor (Patient Orientation)
Checks for patient rotation relative to the couch.
- **Roll Detection**: Quantifies precise patient roll by locating the true axis of bilateral reflection symmetry on the central slice of the series.
- **Radon Transform Sweep**: Performs a fine-grained Radon transform sinogram sweep around the vertical axis (90°) with configurable thresholding and angular resolution.
- **Symmetry Confidence**: Computes normalized cross-correlation between the projection profile and its mirrored counterpart. Requires confidence > 0.95 to trigger.
- **Threshold**: Flags warnings (`ROLL_ALERT`) if roll exceeds 1.5° (or as configured in `ctqa.yaml`).

### 7. Integrity (Protocol & Resolution)
Lead oversight for general clinical standards.
- **Pediatric Check**: Compares parsed `PatientAge` (VR: AS) against protocol/study markers (e.g., "(Child)").
- **Resolution**: Flags series with slice thickness > 3mm (Warning) or > 5mm (Reject).

## Documentation
For detailed information, please refer to the `docs/` directory:
- [API Documentation](docs/API.md)
- [Agent Technical Details](docs/AGENTS_DETAIL.md)
- [Configuration Guide](docs/CONFIGURATION.md)
- [Development & Testing](docs/DEVELOPMENT.md)

## License
MIT License
