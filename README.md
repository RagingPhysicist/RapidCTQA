# RapidCTQA

RapidCTQA is a specialized automated Quality Assurance (QA) tool for radiotherapy treatment planning CT datasets. It provides real-time analysis of DICOM series to ensure they meet clinical integrity, geometry, and image quality standards before contouring and planning.

## Features

- **Automated DICOM Ingestion**: Listens for incoming DICOM transfers and automatically triggers QA analysis.
- **Agent-Based Analysis**: Multiple specialized "agents" evaluate different aspects of the CT dataset.
- **Detailed Reporting**: Generates human-readable findings with specific slice-level locations for artifacts and errors.
- **Interactive Dashboard**: Web interface for reviewing studies, results, and generating PDF reports.

## System Architecture

- **Backend**: Python (FastAPI) for the API and QA Engine.
- **DICOM Listener**: Integrated `pynetdicom` server for seamless ingestion from PACS/CT Scanners.
- **QA Engine**: Core logic that computes metrics and evaluates clinical rules.
- **Frontend**: Clean, static web interface for study management and review.

## Installation & Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
2.  **Configuration**:
    - Edit `webApp.yaml` to configure storage paths and listener ports.
    - Adjust clinical thresholds in `ctqa.yaml`.
3.  **Run the Application**:
    ```bash
    python run.py
    ```
    The API will be available at `http://localhost:8000` and the DICOM listener at port `11112`.

## QA Agents & Logic

### 1. GeometryGuardian (Geometry & Truncation)
Ensures the patient anatomy is fully contained within the reconstruction Field of View (FOV) and checks for geometry integrity.
- **Truncation Detection**: Scans the image matrix perimeter (3px buffer). If more than 5 pixels exceed -200 HU on any slice, a `TRUNCATION_ERROR` is flagged with the affected slice range.
- **Monotonicity**: Verifies that slice positions are strictly increasing or decreasing.
- **Spacing Variation**: Flags series where slice spacing variation exceeds 1.0mm.
- **Gantry Tilt**: Flags gantry tilts exceeding 1.0°.

### 2. NoiseWhisperer (Image Quality)
Analyzes background air and calibration.
- **Background Noise**: Calculates the Standard Deviation (SD) of 20x20px regions in the four corners of the volume. Flags if SD > 15.0 HU.
- **Air Calibration**: Estimates Air HU from the 1st percentile of voxels. Flags if outside the [-1100, -900] HU range.

### 3. FluidPhysicist (HU Accuracy)
Validates CT number consistency using internal biological markers.
- **Water Consistency**: Evaluates the median HU of fluid (bladder/soft tissue). Optimally 0-35 HU. Flags if > 45 HU (potential calibration error).
- **Rescale Slope**: Ensures the DICOM Rescale Slope is non-zero.

### 4. CavityScout (Air & Gas Auditor)
Detects large gas pockets (e.g., in bowel or stomach) that may impact dosimetry.
- **Gas Volume**: Isolates voxels < -500 HU within the body mask (HU > -500).
- **Thresholds**: Flags "Moderate gas" if > 15cc and "Excessive gas" if > 50cc, including the specific slice range.

### 5. ImplantAuditor (Metal Detection)
Detects high-density metallic implants or devices.
- **Metal Detection**: Identifies voxels > 2000 HU (configurable) within the body.
- **Volume Threshold**: To avoid false positives from small metallic skin markers, a volume threshold of **0.2 cc** is applied.
- **Reporting**: Identifies the specific slices containing metal to assist in planning and artifact correction.

### 6. Integrity (Protocol & Resolution)
Lead oversight for general series consistency.
- **Pediatric Protocol Check**: Parses the DICOM `PatientAge` (VR: AS) to determine if a patient is a child (< 18Y) or adult. It validates this against "(Child)" or "(Adult)" markers in the `ProtocolName` and `StudyDescription`.
- **Slice Resolution**:
    - **Preferred**: Slice thickness <= 3.0mm.
    - **Limit**: Flags as REJECT if slice thickness > 5.0mm.
- **Series Count**: Ensures a minimum of 5 slices for a valid clinical series.

## License
MIT License
