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

### `thresholds`
| Section | Keys (defaults) |
|---------|------|
| `geometry` | `edge_buffer_px` (3), `min_edge_pixels` (5), `torso_core_opening_mm` (30), `max_lateral_truncation_z_mm` (12), `max_slice_spacing_variation_mm` (1.0), `max_gantry_tilt_deg` (1.0) |
| `slice_thickness` | `nominal_mm` (null), `tolerance_mm` (0.5), `absolute_max_mm` (5.0) |
| `integrity` | `min_slice_count` (5), `adult_age_years` (18) |
| `noise` | `corner_roi_px` (20), `max_background_air_sd_hu` (15) |
| `hu` | `air_range` ([-1100, -900]) |
| `fluid` | `search_range_hu` ([0, 30]), `fallback_search_range_hu` ([0, 50]), `optimal_range_hu` ([0, 40]), `conditional_max_hu` (50) |
| `gas` | `info_max_cc` (30), `large_cc` (75), `reject_cc` (150, `null` = never), `max_gas_body_fraction` (0.10), `couch_exclusion_mm` (15), `pelvis_keywords`, `air_threshold_hu` (-500), `min_thickness_mm` (4), `min_depth_mm` (15), `skin_hu` (-200), `cleft_closing_mm` (5), `cleft_fraction` (0.5) |
| `implants` | `metal_threshold_hu` (3000), `internal_info_max_cc` (2), `surface_info_max_cc` (10), `external_info_max_cc` (5), `pelvis_internal_conditional_cc` (5), `pelvis_keywords`, `internal_margin_mm` (10), `marker_max_volume_cc` (0.1) |
| `alignment` | `info_deg` (1.5), `conditional_deg` (3.0), `min_correlation` (0.90), `search_range_deg` (30), `step_deg` (0.25), `edge_margin_deg` (0.5), `hu_floor` (-300), `hu_ceiling` (300), `downsample` (4) |

How the tiers map to flags:
- **Metal** (per class: internal / surface / external): none `ACCEPT`, below `<class>_info_max_cc` `INFO`, at or above `CONDITIONAL`. On scans matching `implants.pelvis_keywords`, internal metal at or above `pelvis_internal_conditional_cc` is `CONDITIONAL` even if an override raised the internal limit.
- **Gas candidates**: air below `air_threshold_hu` inside the body mask. Components connected to outside air, thinner than `min_thickness_mm`, shallower than `min_depth_mm`, or lying in a skin concavity (`skin_hu`, `cleft_closing_mm`, `cleft_fraction`) are not counted. See [AGENTS_DETAIL.md](AGENTS_DETAIL.md#4-cavityscout); `tools/gas_debug.py` prints every component with its reason for tuning on real cases.
- **Gas**: below `info_max_cc` `INFO`, from `info_max_cc` `CONDITIONAL` ("large" above `large_cc`), above `reject_cc` `REJECT`. If gas exceeds `max_gas_body_fraction` of the evaluated body volume, the body mask is suspect: one `CONDITIONAL` sanity flag, and the gas value is reported as unreliable `INFO`.
- **Slice thickness**: above `absolute_max_mm` `REJECT`; otherwise `CONDITIONAL` only when `|measured - nominal_mm| > tolerance_mm`. With `nominal_mm: null` there is no thickness warning.
- **Truncation**: anterior/posterior contact `REJECT`; lateral torso contact `CONDITIONAL` up to `max_lateral_truncation_z_mm` of z-extent (affected slices × slice spacing), `REJECT` beyond; arm/elbow with the torso clear and accessory-only contact are `INFO`.
- **Roll**: `|roll|` above `info_deg` `INFO`, above `conditional_deg` `CONDITIONAL`; correlation below `min_correlation` or a best angle within `edge_margin_deg` of `search_range_deg` gives an "unreliable" `INFO`.

### `protocol_overrides`
A list of `{match, thresholds}` entries. Every entry whose `match` occurs in the series' ProtocolName (case-insensitive substring) is deep-merged over `thresholds`, in file order, with later entries winning. Overrides use the same keys and are validated at startup. The keys applied to a series are recorded in its `protocol_overrides_applied` metric.

```yaml
protocol_overrides:
  - match: ABD                     # bowel gas is expected: never reject on volume
    thresholds:
      gas: {reject_cc: null}
  - match: HEAD
    thresholds:
      slice_thickness: {nominal_mm: 2.0}
  - match: THORAX
    thresholds:
      slice_thickness: {nominal_mm: 3.0, tolerance_mm: 0.5}
```

The shipped file only contains the `ABD` gas override. Nominal slice thicknesses are site protocol settings: add one entry per protocol.

### `display`
On-screen report settings (web dashboard, web viewer, desktop cockpit). The PDF report and the stored `qa_result.json` always list every check.
- `screen_hidden_statuses` (default `[PASS, ACCEPT, INFO]`): flag statuses not shown on screen. `SKIPPED` stays visible but muted. `CONDITIONAL` / `REJECT` can never be hidden (startup error).
- `show_passed_summary` (default `true`): show one muted line "N checks passed - full list in PDF" under the findings.

The PDF lists all checks in three groups: findings requiring attention (`CONDITIONAL` / `REJECT`), passed checks (`ACCEPT` / `INFO` with their measured values), and skipped checks with the reason.

### Status values
Flags and series use one vocabulary, defined in `backend/status.py`: `ACCEPT`, `CONDITIONAL`, `REJECT`, plus `INFO` (reported value, never escalates) and `SKIPPED` (check not applicable) on flags, and `PENDING` / `INGESTING` (dashboard lifecycle). Results and logs written by older versions (`PASS`, `PASS_WITH_WARNING`, `FAIL_CRITICAL`) are converted when read, and those names are still accepted as API filter values.

Every check emits one flag per series, every time. Only `CONDITIONAL` and `REJECT` escalate the series verdict, and only results that contain them are written to the problem log (`logs/`); a result re-analysed without them is removed from it. Log records keep the full flag list, but only `CONDITIONAL` / `REJECT` flags count as `issues` and match the issue-type filter.
