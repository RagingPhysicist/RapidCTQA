# RapidCTQA API Documentation

The RapidCTQA backend is built with FastAPI and provides endpoints for monitoring ingestion, retrieving study results, and managing QA workflows.

## Base URL
The default base URL for the API is `http://localhost:8080/api`.

## Access control
- Only clients in `security.allowed_clients` (`webApp.yaml` / `webApp.local.yaml`) are served; others get `403`.
- Every `POST` must send the header `X-RapidCTQA-Request: 1`. Cross-site requests (an `Origin` other than the server or `security.allowed_origins`) are refused with `403`.
- `series_uid` path parameters must be DICOM UIDs (digits and dots, max 64 characters); anything else is `400`.

## Status values
`ACCEPT`, `CONDITIONAL`, `REJECT` for flags and series verdicts; `INFO` (reported value, never escalates) and `SKIPPED` (not applicable) on flags only; `PENDING` / `INGESTING` for series not analysed yet. See `backend/status.py`. Legacy values (`PASS`, `PASS_WITH_WARNING`, `FAIL_CRITICAL`) are still accepted as `status` filters for `/api/logs`.

Every check returns one flag per series, so `flags` in `/api/studies/{series_uid}` lists all checks, not only problems. `/api/logs` only contains results with `CONDITIONAL` / `REJECT` flags.

## Endpoints

### 1. Ingestion Status
**GET** `/api/status`

Returns the current status of the DICOM listener and processing queue.

**Response Body:**
```json
{
  "active_transfers": 0,
  "queue_size": 5,
  "processed_today": 12
}
```

---

### 2. List Studies
**GET** `/api/studies`

Retrieves a summary of all studies currently in the local storage. If a study has not been analyzed yet, it triggers the analysis in the background.

**Response Body (List of objects):**
```json
[
  {
    "series_uid": "1.2.840.113619...",
    "patient_name": "DOE^JOHN",
    "patient_id": "12345",
    "protocol": "CT_Pelvis_(Adult)",
    "study_date": "20231027",
    "modality": "CT",
    "status": "ACCEPT",
    "instance_count": 120
  }
]
```

---

### 3. Study Detail
**GET** `/api/studies/{series_uid}`

Returns detailed QA results, metrics, and flags for a specific series.

**Path Parameters:**
- `series_uid` (string): The DICOM Series Instance UID.

**Response Body:**
Includes `metrics` (numeric values) and `flags` (list of warnings/errors).

---

### 4. Run Validation
**POST** `/api/validate/{series_uid}`

Manually triggers the QA analysis for a specific series.

**Path Parameters:**
- `series_uid` (string): The DICOM Series Instance UID.

---

### 5. Approve / Reject
**POST** `/api/viewer/{series_uid}/approve`: copies the series to `TPS_EXPORT` and routes it to the active destinations in `dest.json`. Refused with `409` while the series is still being received.

**POST** `/api/viewer/{series_uid}/reject`: deletes the series, its export and its PDF report, and appends to `rejections.log`.

---

### 6. PDF Report
**GET** `/api/reports/{series_uid}/pdf`

Generates and downloads a PDF QA report for the specified series.

**Path Parameters:**
- `series_uid` (string): The DICOM Series Instance UID.

**Response:** `application/pdf` file stream.

## Authentication
There is no per-user authentication. Access is limited by client IP and the request guards above. For user accounts, put the dashboard behind an authenticating reverse proxy.

## Static Frontend
The root path `/` and `/static/*` serve the web dashboard.
