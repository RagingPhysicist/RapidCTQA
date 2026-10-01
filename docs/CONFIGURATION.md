# Configuration Guide

RapidCTQA reads three configuration files:

| File | Committed | Purpose |
|------|-----------|---------|
| `webApp.yaml` | yes | Operational defaults (ports, storage, security). Site-neutral and safe by default. |
| `webApp.local.yaml` | **no** (git-ignored) | Site overrides merged on top of `webApp.yaml`: storage share, network exposure, allowed DICOM senders. Start from `webApp.local.example.yaml`. |
| `ctqa.yaml` | yes | Clinical thresholds used by the QA agents. |

Environment variables override both web config files: `RAPIDCTQA_STORAGE_DIR`, `RAPIDCTQA_LOGS_DIR`, `RAPIDCTQA_EXPORT_DIR`, `RAPIDCTQA_REPORTS_DIR` and `RAPIDCTQA_CONFIG_LOCAL` (path of the local override file).

## webApp.yaml / webApp.local.yaml

### `backend.api`
- `host`: Bind address of the dashboard and API. Default `127.0.0.1` (this machine only). Set `0.0.0.0` in the local file to serve other workstations, together with `security.allowed_clients`.
- `port`: Default `8080`.

### `backend.dicom_listener`
- `host`, `port`, `aet`: Bind address, port (default `11112`) and AE title (default `RT_QA_SCP`) of the C-STORE SCP.
- `allowed_calling_aets`: AE titles allowed to associate. Empty means any; a warning is printed at startup.
- `allowed_peers`: Sender IP addresses / CIDR ranges allowed to store. Empty means any.
- `stability_seconds`: A series is analysed once every association that delivered files for it has closed **and** no new file has arrived for this many seconds (default `30`). A sender that pauses mid-transfer is not analysed as a partial series.

Incoming objects whose `SeriesInstanceUID` or `SOPInstanceUID` is not a plain DICOM UID (digits and dots) are refused, because these values become folder and file names.

### `backend.storage`
- `path`: Directory for received series. Relative paths are resolved against the repository root. Default `data/rtct`.
- `path_aliases`: Lets one config address the same share from different machines. Each entry is `{from: <prefix>, to: [<candidates>]}`. The first candidate that exists replaces the prefix. If none is reachable, startup fails with a clear error instead of silently writing to a local folder:
  ```yaml
  path: "\\\\fileserver\\DICOM\\rapidqa"
  path_aliases:
    - from: "\\\\fileserver\\DICOM"
      to: ["\\\\fileserver\\DICOM", "S:", "/Volumes/DICOM"]
  ```
- `retention_days`: At startup, series folders not modified within this many days are deleted (`1` keeps today's series only, `0` disables cleanup). Only folders whose name is a DICOM UID are ever touched.

### `security`
- `allowed_clients`: IP addresses / CIDR ranges allowed to use the dashboard and API. Default: loopback only.
- `allowed_origins`: Additional browser origins allowed to call the API cross-site. The dashboard itself needs none.

All state-changing requests (`POST`) must carry the header `X-RapidCTQA-Request: 1` and, if the browser sends an `Origin`, it must be the server itself or an allowed origin. This stops other web pages open in a clinician's browser from approving or deleting series.

There is no per-user authentication; anyone on an allowed client can approve and reject. If you need user accounts or an audit trail of who decided, put the dashboard behind your hospital's authenticating reverse proxy.

---

## ctqa.yaml
Every key in this file is read by the engine and validated at startup (`backend/qa_config.py`). Unknown or misspelled keys stop the application, so an edit either takes effect or fails loudly. The checks themselves are implemented in `backend/agents/*.py`; [AGENTS_DETAIL.md](AGENTS_DETAIL.md) lists which key drives which check.

| Section | Keys |
|---------|------|
| `geometry` | `edge_buffer_px`, `min_edge_pixels`, `lateral_tolerance_mm.{lenient,head_neck,default}`, `accessory_lateral_tolerance_mm`, `lenient_protocol_keywords`, `head_neck_keywords`, `max_slice_spacing_variation_mm`, `max_gantry_tilt_deg` |
| `slice_thickness` | `preferred_max_mm`, `absolute_max_mm` |
| `integrity` | `min_slice_count`, `adult_age_years` |
| `noise` | `corner_roi_px`, `max_background_air_sd_hu` |
| `hu` | `air_range` |
| `fluid` | `optimal_range_hu`, `conditional_max_hu` |
| `gas` | `conditional_cc`, `reject_cc`, `leak_cc`, `couch_exclusion_mm`, `pelvis_keywords` |
| `implants` | `metal_threshold_hu`, `max_volume_cc`, `internal_margin_mm`, `marker_max_volume_cc` |
| `alignment` | `max_allowable_tilt_deg`, `min_confidence`, `symmetry_gate`, `hu_floor`, `angular_step_deg` |

### Status values
Flags and series use one vocabulary, defined in `backend/status.py`: `ACCEPT`, `CONDITIONAL`, `REJECT`, plus `SKIPPED` (informational flag) and `PENDING` / `INGESTING` (dashboard lifecycle). Results and logs written by older versions (`PASS`, `PASS_WITH_WARNING`, `FAIL_CRITICAL`) are converted when read.
