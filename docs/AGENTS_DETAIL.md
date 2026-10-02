# QA Agents: Technical Details

RapidCTQA uses a modular agent-based architecture to evaluate DICOM series. Each agent lives in `backend/agents/<agent>.py` and has a `compute` step (metrics) and an `evaluate` step (flags). Every limit below is a key in `ctqa.yaml` (shown in brackets); the numbers are the shipped defaults, and `protocol_overrides` can change them per protocol.

**Body mask**: when TotalSegmentator is installed, the engine runs its `body` task on each series and uses that mask for all agents; otherwise (or if it fails) it falls back to the rule-based segmentation that excludes the couch. With the TotalSegmentator mask, accessory/couch truncation is not reported (`metrics.used_totalsegmentator`).

Every check emits exactly one flag per series, with the measured value and limit in its message: `ACCEPT`, `INFO` (reported, not actionable), `CONDITIONAL`, `REJECT`, or `SKIPPED` (not applicable). The series verdict is `REJECT` if any flag rejects, otherwise `CONDITIONAL` if any flag needs review, otherwise `ACCEPT`; `INFO` never escalates. Only `ACCEPT` series are exported to the TPS automatically, and only `CONDITIONAL` / `REJECT` results go to the problem log.

## 1. GeometryGuardian
Ensures that the physical geometry of the scan is correct and that the patient is fully captured.
- **Truncation Detection**: Checks whether the filled patient body mask touches the outermost ring of the image matrix [`geometry.edge_buffer_px`: 3 px]; fewer than [`geometry.min_edge_pixels`: 5] contact pixels are ignored.
  - Contact in the anterior or posterior sector (outside 315°–45° and 135°–225° around the image centre) is always a `TRUNCATION_ERROR` (`REJECT`), on any number of slices.
  - Lateral-only contact: the **torso core** is the body mask after a morphological opening with a disk of [`geometry.torso_core_opening_mm`: 30 mm] diameter, keeping the largest component. If the core touches the border, it is lateral torso truncation: `CONDITIONAL` while its z-extent (affected slices × slice spacing) is at most [`geometry.max_lateral_truncation_z_mm`: 12 mm], otherwise `TRUNCATION_ERROR` (`REJECT`).
  - If the core does not touch the border, the contact is an arm/elbow: `INFO`.
  - Couch / accessory contact only: `INFO` (not reported with a TotalSegmentator mask).
  - The flag reports the maximum contact length along the border (mm), the z-extent and the affected slices. Validated empty slices are skipped.
- **Slice Spacing**: Variation in slice spacing above [`geometry.max_slice_spacing_variation_mm`: 1.0 mm] is a rejection.
- **Monotonicity**: Verifies that slice positions ($z$-axis) strictly increase or decrease.
- **Duplicates**: Duplicate slice positions are a rejection.
- **Gantry Tilt**: Tilt above [`geometry.max_gantry_tilt_deg`: 1.0°] is `CONDITIONAL`.
- **Empty Slices**: Over-range air slices are listed as `INFO` (`EMPTY_SLICE`).

## 2. NoiseWhisperer
Analyzes the technical quality of the image acquisition.
- **Background Noise**: Mean standard deviation of square background-air ROIs in the four image corners [`noise.corner_roi_px`: 20 px]. `CONDITIONAL` above [`noise.max_background_air_sd_hu`: 15 HU].
- **Calibration**: Estimates the HU value of air using the 1st percentile of voxels. `REJECT` outside [`hu.air_range`: -1100 to -900 HU].

## 3. FluidPhysicist
Validates Hounsfield Unit (HU) accuracy using internal biological markers.
- **HU Consistency**: Median of body voxels in [`fluid.search_range_hu`: 0–30 HU] (isolates fluid/urine from dense soft tissue), falling back to [`fluid.fallback_search_range_hu`: 0–50 HU] when none are found.
- **Evaluation**: Median inside [`fluid.optimal_range_hu`: 0–40 HU] passes, up to [`fluid.conditional_max_hu`: 50 HU] is `CONDITIONAL`, anything else is `REJECT` (calibration drift).
- **IV Contrast**: When `ContrastBolusAgent` is set the check is `SKIPPED` (also when no fluid-range voxels exist).
- **Metadata**: Ensures the `RescaleSlope` is non-zero.

## 4. CavityScout
Detects air pockets within the patient, which can significantly affect dose calculation in radiotherapy.
- **Scope**: Pelvis / abdomen scans only (protocol, study description or body part matching [`gas.pelvis_keywords`]), inferior half of the series.
- **Candidates**: air (HU below [`gas.air_threshold_hu`: -500]) inside the patient mask, excluding the couch interface (bottom [`gas.couch_exclusion_mm`: 15 mm] of the mask on each slice). The same cleaning runs whether the mask comes from TotalSegmentator (which includes air between the thighs and in the gluteal cleft) or from the rule-based segmentation. Each 3D candidate component (6-connectivity) is checked in this order:
    1. `exterior_connected`: part of air that touches the in-plane image border, or touches the first/last slice and the couch, anywhere in the volume. A pocket that is closed on some slices but open on others is outside air.
    2. `sheet_like`: thickness (2 × the component's maximum distance transform) below [`gas.min_thickness_mm`: 4 mm].
    3. `too_shallow`: 90th-percentile in-plane depth from the nearest non-body or exterior-air voxel below [`gas.min_depth_mm`: 15 mm].
    4. `cleft`: at least [`gas.cleft_fraction`: 50%] of it lies in a skin concavity. The concavity is the convex hull of the skin silhouette (HU ≥ [`gas.skin_hu`: -200], holes filled) minus the silhouette's [`gas.cleft_closing_mm`: 5 mm] closing. This catches a bay sealed only by partial-volume voxels; enclosed rectal gas lies inside the silhouette.
    5. Otherwise `kept`: counted as gas.
- **Diagnostics**: `metrics.gas_rejected_cc` (also in the PDF), and `metrics.gas_components`, which lists the 50 largest components with volume, centroid (slice / y / x), depth, thickness and reason. `python tools/gas_debug.py <series_dir>` prints them for tuning.
- **Thresholds**:
    - **Physiological** (`INFO`): volume below [`gas.info_max_cc`: 30 cc]; no gas is `ACCEPT`.
    - **Moderate / large** (`CONDITIONAL`): from 30 cc; "large" above [`gas.large_cc`: 75 cc].
    - **Excessive** (`REJECT`): volume above [`gas.reject_cc`: 150 cc]. The shipped `ABD` protocol override sets it to `null`, so abdomen scans are never rejected on volume.
- **Body-mask sanity** (replaces the old `SEGMENTATION_LEAK` reject): if gas exceeds [`gas.max_gas_body_fraction`: 10%] of the body volume in the evaluated slices, or there is no body there, the mask is suspect. The result is a `CONDITIONAL` `BODY_MASK_SANITY` flag, and the gas volume is reported as unreliable `INFO`.
- **Reporting**: The slice ranges in the flag come from the kept components only.

## 5. ImplantAuditor
Detects and classifies high-density metallic objects.
- **Threshold**: Detects voxels above [`implants.metal_threshold_hu`: 3000 HU].
- **Per-class tiers**: none `ACCEPT`; below the class limit `INFO`; at or above `CONDITIONAL`. Limits: internal [`implants.internal_info_max_cc`: 2 cc], surface [`implants.surface_info_max_cc`: 10 cc], external [`implants.external_info_max_cc`: 5 cc].
- **Pelvis**: on scans matching [`implants.pelvis_keywords`], internal metal of [`implants.pelvis_internal_conditional_cc`: 5 cc] or more is always `CONDITIONAL`, even if a protocol override raised the internal limit.
- **4DCT**: metal is evaluated once per group, on the reference phase (the one used for segmentation). The other phases carry a single `INFO` flag pointing to it, and their verdict, log entry and report are updated when the group is detected.
- **Classification Strategy**:
    - **Body Masking**: Identifies the patient as the largest connected component.
    - **Interior Buffer**: Erodes the filled patient mask by [`implants.internal_margin_mm`: 10 mm] to define the "internal" volume.
    - **Set-up Markers**: A three-point pattern of small surface components (each below [`implants.marker_max_volume_cc`: 0.1 cc]) is recognised as skin markers and excluded.
    - **Internal**: Metal found inside the 10mm buffer.
    - **Surface**: Metal found between the patient skin and the 10mm buffer.
    - **External**: Metal found outside the patient mask.

## 6. AlignmentAuditor
Detects if the patient is rotated relative to the scanner's coordinate system.
- **Roll Calculation** (central slice, mirror symmetry):
  1. Take the largest connected component of the body mask.
  2. Weight = clip(HU, [`alignment.hu_floor`: -300], [`alignment.hu_ceiling`: 300]) − floor, with background 0.
  3. Shift the weighted centroid to the image centre and downsample by [`alignment.downsample`: 4].
  4. Mirror left-right and rotate the mirror image over ±[`alignment.search_range_deg`: 30°] in [`alignment.step_deg`: 0.25°] steps, keeping the angle with the highest correlation to the original (refined by a parabolic fit). A body rolled by θ has a mirror rolled by −θ, so roll = best angle / 2.
  - **Sign convention** (unchanged): positive roll = clockwise as displayed.
  - Unlike the previous Radon sweep, the estimate does not depend on where the patient lies in the FOV. A centred, unrotated body reads 0°, where the old sweep read its +10° limit.
- **Reliability**: correlation below [`alignment.min_correlation`: 0.90], or a best angle within [`alignment.edge_margin_deg`: 0.5°] of the search limit, gives an "unreliable" `INFO` instead of an alert.
- **Evaluation**: `|roll|` above [`alignment.info_deg`: 1.5°] is `INFO`, above [`alignment.conditional_deg`: 3°] `CONDITIONAL` (`ROLL_ALERT`).

## 7. Integrity Agent
General oversight and protocol validation.
- **Pediatric Check**: Parses `PatientAge` (Age String VR) and compares it with [`integrity.adult_age_years`: 18] against "(Child)" or "(Adult)" markers in the `StudyDescription` or `ProtocolName`. A mismatch is `REJECT`.
- **Slice Thickness**: `REJECT` above [`slice_thickness.absolute_max_mm`: 5.0 mm]. Otherwise only deviation from the protocol's nominal is flagged: `CONDITIONAL` when |measured − [`slice_thickness.nominal_mm`]| > [`slice_thickness.tolerance_mm`: 0.5 mm]. The nominal is `null` by default (no warning) and is set per protocol through `protocol_overrides`.
- **Series Count**: Rejects series with fewer than [`integrity.min_slice_count`: 5] slices.
