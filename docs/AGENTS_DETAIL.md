# QA Agents: Technical Details

RapidCTQA uses a modular agent-based architecture to evaluate DICOM series. Each agent is responsible for a specific clinical or technical domain.

## 1. GeometryGuardian
Ensures that the physical geometry of the scan is correct and that the patient is fully captured.
- **Truncation Detection**: Scans the outermost 3 pixels of the image matrix. If $\ge 5$ pixels on any slice exceed -200 HU, it flags a `TRUNCATION_ERROR`.
- **Slice Spacing**: Calculates the variation in spacing between slices ($z$-axis). Variation $> 1.0$ mm triggers a rejection.
- **Monotonicity**: Verifies that slice positions ($z$-axis) strictly increase or decrease.
- **Gantry Tilt**: Flags gantry tilts $> 1.0^{\circ}$ as a conditional warning.

## 2. NoiseWhisperer
Analyzes the technical quality of the image acquisition.
- **Background Noise**: Samples 20x20 pixel regions from the four corners of the volume. It computes the Standard Deviation (SD) in these air regions. Flags if SD $> 15.0$ HU.
- **Calibration**: Estimates the HU value of air using the 1st percentile of voxels. It flags a rejection if this estimate is outside $[-1100, -900]$ HU.

## 3. FluidPhysicist
Validates Hounsfield Unit (HU) accuracy using internal biological markers.
- **HU Consistency**: Identifies voxels in the range $[0, 50]$ HU within the body mask (the "fluid" range).
- **Evaluation**: Flags a conditional warning if the median fluid density is between $35$ and $45$ HU, and a rejection if $> 45$ HU (suggesting significant calibration drift).
- **Metadata**: Ensures the `RescaleSlope` is non-zero.

## 4. CavityScout
Detects air pockets within the patient, which can significantly affect dose calculation in radiotherapy.
- **Detection**: Identifies voxels $< -500$ HU that are inside the body mask ($> -500$ HU).
- **Thresholds**:
    - **Moderate**: Volume $> 15$ cc.
    - **Excessive**: Volume $> 50$ cc.
- **Reporting**: Identifies specific slice ranges containing gas.

## 5. ImplantAuditor
Detects and classifies high-density metallic objects.
- **Threshold**: Detects voxels $> 2000$ HU.
- **Classification Strategy**:
    - **Body Masking**: Identifies the patient as the largest connected component.
    - **Interior Buffer**: Uses a 10mm morphological erosion of the filled patient mask to define the "internal" volume.
    - **Internal**: Metal found inside the 10mm buffer.
    - **Surface**: Metal found between the patient skin and the 10mm buffer.
    - **External**: Metal found outside the patient mask.

## 6. AlignmentAuditor
Detects if the patient is rotated relative to the scanner's coordinate system.
- **Roll Calculation**: Uses the `ImageOrientationPatient` (IOP) tag. It calculates the deviation of the row cosine vector from the ideal $[1, 0, 0]$ vector.
- **Reporting**: Flags warnings if the roll angle exceeds $3.0^{\circ}$ on any slice.

## 7. Integrity Agent
General oversight and protocol validation.
- **Pediatric Check**: Parses `PatientAge` (Age String VR). It validates this against "(Child)" or "(Adult)" markers in the `StudyDescription` or `ProtocolName`.
- **Slice Resolution**:
    - **Preferred**: $\le 3.0$ mm.
    - **Limit**: Flags rejection if $> 5.0$ mm.
- **Series Count**: Rejects series with fewer than 5 slices.
