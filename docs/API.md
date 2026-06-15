# RapidCTQA API Documentation

The RapidCTQA backend is built with FastAPI and provides endpoints for monitoring ingestion, retrieving study results, and managing QA workflows.

## Base URL
The default base URL for the API is `http://localhost:8080/api`.

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

### 5. Launch Cockpit
**POST** `/api/launch_cockpit/{series_uid}`

Launches the `cockpit.py` visualization tool on the server host for the specified series.

**Path Parameters:**
- `series_uid` (string): The DICOM Series Instance UID.

---

### 6. PDF Report
**GET** `/api/reports/{series_uid}/pdf`

Generates and downloads a PDF QA report for the specified series.

**Path Parameters:**
- `series_uid` (string): The DICOM Series Instance UID.

**Response:** `application/pdf` file stream.

## Authentication
Currently, the API does not require authentication (designed for internal clinical network use).

## Static Frontend
The root path `/` and `/static/*` serve the web dashboard.
