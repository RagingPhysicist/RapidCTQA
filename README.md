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
    pip install -r requirements.txt        # add -dev for the test tools
    ```
2.  **Configure your site** (optional): copy `webApp.local.example.yaml` to `webApp.local.yaml` (git-ignored) and set the storage share, which workstations may open the dashboard, and which DICOM senders are allowed. See [Configuration](docs/CONFIGURATION.md).
3.  **Run the Application**:
    ```bash
    python run.py
    ```
    - **Web Dashboard**: `http://localhost:8080` (served to this machine only until `webApp.local.yaml` opens it up)
    - **DICOM Listener**: `0.0.0.0:11112` (AET: Configurable in `webApp.yaml`, defaults to `RT_QA_SCP`)

## QA Verdicts
Every check reports one flag on every run, with the measured value and its limit, so the PDF report lists all tests:

| Flag | Meaning |
|------|---------|
| `ACCEPT` | Check passed. |
| `INFO` | Finding reported with its value, not actionable (e.g. small metal, arm at the FOV edge, mild roll). Never escalates. |
| `CONDITIONAL` | Clinician review required. |
| `REJECT` | Series must not be used (rescan / resend). |
| `SKIPPED` | Check does not apply (e.g. fluid HU with IV contrast). |

A series is `REJECT` if any flag rejects, otherwise `CONDITIONAL` if any flag needs review, otherwise `ACCEPT`. Only `ACCEPT` series are exported to the TPS automatically, and only `CONDITIONAL` / `REJECT` results are written to the problem log.

All limits below are defaults from `ctqa.yaml`, the single source of truth for thresholds. It supports per-protocol overrides (substring match on the protocol name) and is validated at startup, so an unknown key is an error rather than a silently ignored setting.

## QA Agents & Logic

### 1. GeometryGuardian (Geometry & Truncation)
Ensures geometric integrity and FOV coverage.
- **Truncation**: Checks where the patient body touches the FOV border.
  - Anterior/posterior contact: `REJECT`, on any number of slices.
  - Lateral contact where the torso core (body after a 30 mm opening) also touches the border: `CONDITIONAL` up to a 12 mm z-extent, `REJECT` beyond.
  - Lateral contact by an arm/elbow with the torso clear: `INFO`.
  - Accessory/couch contact only: `INFO`.
- **Consistency**: Validates monotonic slice positions and consistent slice spacing.
- **Tilt**: Flags gantry tilt exceeding 1.0°.

### 2. NoiseWhisperer (Image Quality)
Analyzes hardware performance and calibration.
- **Noise**: Measures Standard Deviation in 20x20px background air regions (corners).
- **Air HU**: Estimates Air HU via 1st percentile; flags if outside [-1100, -900] HU.

### 3. FluidPhysicist (HU Accuracy)
Validates CT number consistency using biological markers.
- **HU Consistency**: Median HU of fluid (0–30 HU, falling back to 0–50 HU); review above 40 HU, reject above 50 HU. Skipped with IV contrast.
- **Rescale Slope**: Ensures valid DICOM rescale metadata.

### 4. CavityScout (Air & Gas Auditor)
Detects gas pockets that may impact dose calculation (pelvis/abdomen scans).
- **Logic**: Enclosed air inside the body mask in the inferior half of the series.
- **Thresholds**: `INFO` below 30 cc, `CONDITIONAL` from 30 cc (moderate, large above 75 cc), `REJECT` above 150 cc. Abdomen protocols are never rejected on volume.
- **Body-mask sanity**: If gas exceeds 10% of the evaluated body volume, the body mask is suspect: `CONDITIONAL` to verify the contour, and the gas value is reported as unreliable.

### 5. ImplantAuditor (Metal Detection)
Detects and classifies metallic objects (>3000 HU).
- **Classification**: Distinguishes between internal implants, surface markers, and external objects.
- **Thresholds**: `INFO` below 2 cc internal, 10 cc surface, 5 cc external; `CONDITIONAL` at or above. Pelvis scans: internal metal of 5 cc or more is always `CONDITIONAL`.
- **4DCT**: Metal is evaluated once per group, on the reference phase; the other phases report a single `INFO` flag.
- **Validation**: Uses morphological erosion to define an internal body buffer.

### 6. AlignmentAuditor (Patient Orientation)
Checks for patient roll on the central slice.
- **Method**: Weights the largest body component by clipped HU, centres it, mirrors it left-right and finds the rotation (±30°) of the mirror image that best correlates with the original; roll is half that angle. Positive roll is clockwise as displayed.
- **Thresholds**: `INFO` above 1.5°, `CONDITIONAL` (`ROLL_ALERT`) above 3°.
- **Reliability**: A correlation below 0.90 or a best angle at the search limit gives an "unreliable" `INFO` instead of an alert.

### 7. Integrity (Protocol & Resolution)
Lead oversight for general clinical standards.
- **Pediatric Check**: Compares parsed `PatientAge` (VR: AS) against protocol/study markers (e.g., "(Child)").
- **Slice Thickness**: `REJECT` above 5 mm. Below that, only deviation from the protocol's nominal thickness (set per protocol in `ctqa.yaml`, ±0.5 mm) is `CONDITIONAL`. Without a nominal, there is no thickness warning.

## Security
RapidCTQA handles patient data and can delete series and push them to the TPS, so the defaults are conservative:
- The dashboard/API only answers clients in `security.allowed_clients` (loopback by default) and refuses cross-site `POST`s.
- Series UIDs are validated before they are used as paths, both in the API and in the DICOM listener; the listener can be limited to known AE titles and sender IPs.
- Startup cleanup only removes UID-named series folders older than `storage.retention_days`.
- `logs/`, `webApp.local.yaml` and spreadsheets are git-ignored, as the daily logs contain patient names and IDs.

There are no user accounts. Put the dashboard behind an authenticating reverse proxy if you need to know who approved or rejected a series.

## Documentation
For detailed information, please refer to the `docs/` directory:
- [API Documentation](docs/API.md)
- [Agent Technical Details](docs/AGENTS_DETAIL.md)
- [Configuration Guide](docs/CONFIGURATION.md)
- [Development & Testing](docs/DEVELOPMENT.md)

## License
MIT License
