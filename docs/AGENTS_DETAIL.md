# QA Agents: Technical Details

RapidCTQA uses a modular agent-based architecture to evaluate DICOM series. Each agent lives in `backend/agents/<agent>.py` and has a `compute` step (metrics) and an `evaluate` step (flags). Every limit below is a key in `ctqa.yaml` (shown in brackets); the numbers are the shipped defaults.

Each flag is `ACCEPT`, `CONDITIONAL`, `REJECT` or `SKIPPED` (informational). The series verdict is `REJECT` if any flag rejects, otherwise `CONDITIONAL` if any flag needs review, otherwise `ACCEPT`. Only `ACCEPT` series are exported to the TPS automatically.

## 1. GeometryGuardian
Ensures that the physical geometry of the scan is correct and that the patient is fully captured.
- **Truncation Detection**: Checks whether the filled patient body mask touches the outermost ring of the image matrix [`geometry.edge_buffer_px`: 3 px]; fewer than [`geometry.min_edge_pixels`: 5] contact pixels are ignored.
  - Contact in the anterior or posterior sector is always a `TRUNCATION_ERROR` (`REJECT`).
  - Lateral contact is tolerated up to a protocol-dependent depth [`geometry.lateral_tolerance_mm`]: 15 mm for Thorax/Chest/Breast protocols, 5 mm for Head & Neck, 0 mm otherwise. Tolerated clipping is `CONDITIONAL`.
  - Couch / accessory contact is `CONDITIONAL`; on lenient protocols, accessory clipping deeper than [`geometry.accessory_lateral_tolerance_mm`: 15 mm] escalates to `TRUNCATION_ERROR`.
- **Slice Spacing**: Variation in slice spacing above [`geometry.max_slice_spacing_variation_mm`: 1.0 mm] is a rejection.
- **Monotonicity**: Verifies that slice positions ($z$-axis) strictly increase or decrease.
- **Gantry Tilt**: Tilt above [`geometry.max_gantry_tilt_deg`: 1.0°] is `CONDITIONAL`.

## 2. NoiseWhisperer
Analyzes the technical quality of the image acquisition.
- **Background Noise**: Mean standard deviation of square background-air ROIs in the four image corners [`noise.corner_roi_px`: 20 px]. `CONDITIONAL` above [`noise.max_background_air_sd_hu`: 15 HU].
- **Calibration**: Estimates the HU value of air using the 1st percentile of voxels. `REJECT` outside [`hu.air_range`: -1100 to -900 HU].

## 3. FluidPhysicist
Validates Hounsfield Unit (HU) accuracy using internal biological markers.
- **HU Consistency**: Identifies voxels in the range $[0, 50]$ HU within the body mask (the "fluid" range).
- **Evaluation**: Median fluid density inside [`fluid.optimal_range_hu`: 0–35 HU] passes, up to [`fluid.conditional_max_hu`: 45 HU] is `CONDITIONAL`, anything else is `REJECT` (calibration drift).
- **Metadata**: Ensures the `RescaleSlope` is non-zero.

## 4. CavityScout
Detects air pockets within the patient, which can significantly affect dose calculation in radiotherapy.
- **Scope**: Pelvis / abdomen scans only (protocol, study description or body part matching [`gas.pelvis_keywords`]), inferior half of the series.
- **Detection**: Internal air (< -800 HU, fully enclosed by tissue) inside the patient mask, excluding the couch interface [`gas.couch_exclusion_mm`: 15 mm] and components that leak out of the body on adjacent slices.
- **Thresholds**:
    - **Moderate** (`CONDITIONAL`): volume > [`gas.conditional_cc`: 15 cc].
    - **Excessive** (`REJECT`): volume > [`gas.reject_cc`: 50 cc].
    - **Segmentation leak** (`REJECT`): volume > [`gas.leak_cc`: 100 cc].
- **Reporting**: Identifies specific slice ranges containing gas.

## 5. ImplantAuditor
Detects and classifies high-density metallic objects.
- **Threshold**: Detects voxels above [`implants.metal_threshold_hu`: 3000 HU]. Each class is `CONDITIONAL` when its volume exceeds [`implants.max_volume_cc`: 0.2 cc].
- **Classification Strategy**:
    - **Body Masking**: Identifies the patient as the largest connected component.
    - **Interior Buffer**: Erodes the filled patient mask by [`implants.internal_margin_mm`: 10 mm] to define the "internal" volume.
    - **Set-up Markers**: A three-point pattern of small surface components (each below [`implants.marker_max_volume_cc`: 0.1 cc]) is recognised as skin markers and excluded.
    - **Internal**: Metal found inside the 10mm buffer.
    - **Surface**: Metal found between the patient skin and the 10mm buffer.
    - **External**: Metal found outside the patient mask.

## 6. AlignmentAuditor
Detects if the patient is rotated relative to the scanner's coordinate system.
- **Roll Calculation**: Quantifies precise patient roll by locating the true axis of bilateral reflection symmetry on the central slice of the series, bypassing structural inertia limitations and segmentation noise.
  - **Radon Transform Sweep**: Performs a fine-grained Radon transform sinogram sweep around the vertical axis ($80.0^{\circ}$ to $100.0^{\circ}$) with an angular step resolution (default: $0.1^{\circ}$) and Hounsfield Unit floor threshold (default: -300 HU) to isolate the structural mass from background noise.
  - **Symmetry Confidence**: Computes the normalized cross-correlation between each 1D projection profile and its flipped/mirrored counterpart.
  - **Confidence Filter**: Slices whose best cross-correlation is below [`alignment.symmetry_gate`: 0.90] are `SKIPPED`.
- **Evaluation & Alerts**: `CONDITIONAL` (`ROLL_ALERT`) if the roll exceeds [`alignment.max_allowable_tilt_deg`: 1.5°] and the symmetry confidence is above [`alignment.min_confidence`: 0.95].

## 7. Integrity Agent
General oversight and protocol validation.
- **Pediatric Check**: Parses `PatientAge` (Age String VR) and compares it with [`integrity.adult_age_years`: 18] against "(Child)" or "(Adult)" markers in the `StudyDescription` or `ProtocolName`. A mismatch is `REJECT`.
- **Slice Resolution**: `CONDITIONAL` above [`slice_thickness.preferred_max_mm`: 3.0 mm], `REJECT` above [`slice_thickness.absolute_max_mm`: 5.0 mm].
- **Series Count**: Rejects series with fewer than [`integrity.min_slice_count`: 5] slices.
