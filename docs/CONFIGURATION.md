# Configuration Guide

RapidCTQA uses two primary YAML files for configuration: `webApp.yaml` for system settings and `ctqa.yaml` for clinical thresholds and rules.

## webApp.yaml
This file controls the operational aspects of the application.

### Backend Settings
- `dicom_listener`:
  - `host`: The IP address to bind the DICOM listener to (default: `0.0.0.0`).
  - `port`: The port for the DICOM listener (default: `11112`).
  - `aet`: The Application Entity Title for the SCP (default: `RT_QA_SCP`).
- `storage`:
  - `path`: The local or network directory where received DICOM files are stored.
- `api`:
  - `base_url`: The prefix for all API endpoints (default: `/api`).

### UI Settings
- Defines the layout and routing for the web dashboard widgets.

---

## ctqa.yaml
This file defines the clinical rules and thresholds used by the QA Engine.

### Thresholds
- **Geometry**:
  - `max_slice_spacing_variation_mm`: Maximum allowed jitter in slice spacing (default: `1.0`).
  - `max_gantry_tilt_deg`: Maximum allowed gantry tilt (default: `1.0`).
- **Slice Resolution**:
  - `preferred_max_mm`: Triggers a warning if exceeded (default: `3.0`).
  - `absolute_max_mm`: Triggers a rejection if exceeded (default: `5.0`).
- **HU Integrity**:
  - `air_range`: Expected HU range for air (default: `[-1100, -900]`).
  - `water_range`: Expected HU range for water/fluid (default: `[-35, 35]`).
- **Implants**:
  - `metal_threshold_hu`: HU value above which voxels are considered metal (default: `3000`).
  - `max_volume_cc`: Volume threshold for flagging metal artifacts (default: `0.2`).
- **Alignment**:
  - `max_allowable_tilt_deg`: Maximum allowed patient roll (default: `3.0`).

### Rules
The `rules` section maps specific checks to their severity (Reject, Conditional/Warning, or Accept). The engine evaluates these rules based on the metrics computed during analysis.
