# RapidCTQA compliance gap review

Review date: 2026-10-02. Code reviewed: `main` at commit `bcee363` (app version 1.7.0 in `webApp.yaml`, `pyproject.toml`).

This is a gap review, not a conformity assessment. It does not state or imply that RapidCTQA complies with, or is certified against, any standard or regulation. It is **not legal advice**; regulatory statements (in particular section F) must be confirmed with the institution's regulatory contact and data protection officer.

## 1. Scope and assumptions

**Intended use assumed** (from the brief; not yet written down in the repository): automated pre-check of radiotherapy planning CT series (geometry, noise, gas, metal, truncation, patient roll, fluid density) whose findings are shown to a clinician, who accepts or rejects the series before it is used for contouring and planning. In-house tool of a university radiotherapy physics department in Hungary (EU). It supports, and does not replace, a human decision.

**Reviewed**: `README.md`, `AGENTS.md`, `docs/`, `ctqa.yaml`, `webApp.yaml`, backend (`backend/`), frontend (`frontend/`), desktop viewer (`cockpit.py`), tests (17 test modules, 213 tests passing at review time), git history, `requirements*.txt`, and the local problem-log export `errors.xlsx` (row counts and message categories only; it is git-ignored and contains no patient names in the rows inspected).

**Guidance referenced** (by name and topic only; check the current edition before relying on this review):

| Guidance | Edition considered | Note |
|---|---|---|
| AAPM TG-66, QA for CT simulators and the CT-simulation process | Mutic et al., Med Phys 2003 | An update, **TG-66U1 (AAPM Report 83.B, Med Phys 2026)**, has been published. Its full text was not accessible during this review: verify against current edition. |
| AAPM TG-201, quality management of external beam therapy data transfer | Rapid communication (JACMP 2011) and full report (Med Phys 2021) | Verify against current edition. |
| AAPM TG-100, application of risk analysis methods to radiation therapy QM | Huq et al., Med Phys 2016 | FMEA with severity (S), occurrence (O), detectability (D) on 1–10 scales, RPN = S×O×D. |
| IEC 62304, medical device software life cycle | Edition 1 with Amendment 1 (2015) | Edition 2 ("health software") was reported at FDIS stage in 2026 with a simplified classification; verify against current edition. |
| IEC 62366-1, usability engineering | 2015 with Amendment 1 (2020) | Verify against current edition. |
| EU MDR 2017/745, Article 5(5) health-institution exemption | MDCG 2023-1 guidance (Jan 2023) | Conditions paraphrased from secondary summaries; verify against the Regulation and MDCG 2023-1 text. |
| GDPR (Regulation (EU) 2016/679) and Hungarian health-data law | — | Hungarian retention rules not assessed; ask the DPO. |

**Could not assess**:
- **Deployment specifics:** the Windows server, network segmentation, backup, and the site `webApp.local.yaml` / `dest.json` on the production host. Local copies exist on the development machine but are site configuration and are deliberately not described here, because this file is in a public repository.
- **Validation and outcomes:** clinical performance, since no labelled validation data exist in the repository, and actual clinical workflow use (who approves, how often, what happens downstream in the TPS).
- **TPS-side behaviour:** what the TPS does with duplicate or partial series.
- **Institutional QMS documents, SOPs and the risk file:** none found in the repository.
- **TG-66U1 content:** not accessible.

**Assumption**: software safety classification, MDR applicability and whether the tool is a "device" at all are institutional decisions; where unsure whether a requirement applies to an in-house research tool, this review says so.

## 2. Summary: top 10 gaps

Ordered by patient-safety impact, then effort (lower effort first among similar impact).

| # | Gap | Area | Why it matters | Effort |
|---|---|---|---|---|
| 1 | **ACCEPT series are exported and DICOM-routed to the TPS automatically, without clinician review** (`backend/state.py:141-145`). | C, B, D | Contradicts the intended use (clinician decides before data reaches the TPS). Any false negative, partial series or wrong-patient series that scores ACCEPT reaches planning unreviewed. | Low: make auto-export opt-in/off by default and require approval. |
| 2 | **Approve is possible for any series status** (REJECT, PENDING/unanalysed, analysis failed), with no confirmation, no reason, no user identity and no record (`backend/routers/viewer.py:315-330`, `frontend/app.js:621-626`). | C, E, G | A rejected or never-analysed series can be sent to the TPS with one click; nothing records who approved what. | Low–medium. |
| 3 | **DICOM send failures and partial sends are not detected or surfaced**: per-image failures are only counted and printed, exceptions are swallowed, and approval reports "approved and routed" regardless (`backend/dicom_sender.py:57-72`, `backend/routers/viewer.py:327-328`). No post-transfer verification (e.g. storage commitment or query of the TPS). | B | The TPS may hold an incomplete series while the UI says it was sent. | Medium. |
| 4 | **No series-completeness check**: there's no expected-image-count check. Slices with a different matrix and localizers are silently dropped from analysis without a flag (`backend/engine.py:77-86`). Files arriving after analysis are exported unanalysed (whole folder copied, `backend/state.py:143`). Ignored objects are acknowledged as stored (`backend/listener.py:123-125`). | B, C | A partial or mixed series can be analysed as if complete and exported. Spacing/duplicate checks catch internal gaps but not missing ends of the scan. | Medium. |
| 5 | **Missing patient-identity and geometry consistency checks**: PatientID/Name, StudyInstanceUID, FrameOfReferenceUID and ImageOrientationPatient are not checked for consistency across slices. PatientPosition (HFS/FFS/HFP…) is not checked against the protocol, and oblique/non-axial orientation isn't flagged. | A | Wrong-patient or wrongly oriented data are among the most severe planning errors; none of the current checks would catch them. | Low–medium. |
| 6 | **No clinical validation of the algorithms**: thresholds were set or tuned on synthetic phantoms. The problem log contains only flagged cases (no negatives), and its messages predate the current status vocabulary and thresholds (`errors.xlsx`). | D, C | Sensitivity, specificity and false-negative rates are unknown, so the "support a human decision" claim cannot be quantified. | High (see section 5). |
| 7 | **Results are not traceable to software and configuration**: `QAResult` stores no software version, commit or `ctqa.yaml` hash/version (`backend/models.py:19-25`). There are no release tags in git, and threshold changes leave no audit trail beyond git history. | D | After a threshold change you cannot tell which rules produced a given decision, and you cannot validate a release. | Low. |
| 8 | **No user authentication or user-level audit**: access control is by client IP allow-list plus a CSRF header (`backend/security.py:75-91`, `webApp.yaml:33`). HTTP and DICOM are unencrypted. The rejection log has no user (`backend/routers/viewer.py:356`), and approvals are not logged at all. | G, D | Anyone on an allowed workstation can approve or delete series; no accountability for decisions. | Medium (reverse proxy with SSO is the usual route). |
| 9 | **Personal data spread and retention**: patient name and ID are written to daily logs (`backend/logger.py:69-70`), PDFs (`backend/reporter.py:122`), PDF download filenames (`backend/routers/reports.py:71`) and `qa_result.json`. `logs/` and `reports/` are never purged (only storage and exports have retention, `backend/state.py:248`). TotalSegmentator sends run metadata to an external statistics server by default, and it is enabled on the development host. | G | GDPR minimisation and storage limitation; unplanned data egress. | Low–medium. |
| 10 | **Life-cycle documentation absent**: there's no intended-use statement, requirements specification, traceability to tests, risk-management file, SOUP list with pinned versions (TotalSegmentator and model weights unpinned, most dependencies range-pinned, `requirements.txt`), release procedure or known-anomaly list. | D, F | Needed for IEC 62304-proportionate practice and for the MDR Article 5(5) documentation if the exemption is used. | Medium. |

Also notable:
- Unreliable roll estimates and unreliable gas values are `INFO`, which is hidden on screen by default (`ctqa.yaml:98`), so a clinician sees "No issues detected." while the PDF shows the estimate was unreliable (E).
- A failed re-analysis leaves the previous cached result in place, which can be stale (C).
- The default TCP port and "any calling AE" listener configuration (`webApp.yaml:19-23`) rely on network segmentation (B, G).

## 3. Findings by area

Status values: Met / Partial / Not met / Not applicable / Cannot assess.

### A. AAPM TG-66 (CT simulator and CT-simulation process QA)

TG-66 is mainly about equipment QA with phantoms (lasers, couch, image quality, CT-number calibration) plus the simulation process. RapidCTQA works per patient series, so it complements but **cannot replace** the scanner QA programme. "Met" below means the per-series topic is covered by a check, not that the TG-66 equipment tests are fulfilled.

**Mapping of existing checks to TG-66 topics**

| RapidCTQA check (code) | TG-66 topic(s) | Notes |
|---|---|---|
| Slice spacing variation, monotonic z, duplicate positions (`backend/agents/geometry.py:158`, evaluate) | Slice thickness/spacing; DICOM consistency | Detects internal gaps and duplicates, not missing ends of the scan. |
| Slice thickness: absolute limit and per-protocol nominal (`backend/agents/integrity.py:86-100`) | Slice thickness; scan parameters vs protocol | Nominal values per protocol are not yet configured (`ctqa.yaml`, `protocol_overrides` has none), so only the 5 mm limit applies. |
| Gantry tilt (`backend/agents/geometry.py`) | Scan parameters; geometric accuracy | Uses header value only. |
| Slice count (`backend/agents/integrity.py:75-80`) | Scan parameters | Minimum count only, no coverage check. |
| Air HU (1st percentile), fluid median HU, RescaleSlope (`backend/agents/noise.py:28-29`, `backend/agents/fluid.py`) | HU / density calibration (sanity only) | Not a calibration check: no phantom, no HU-to-density curve. Fluid check skipped with contrast. |
| Background air noise SD (`backend/agents/noise.py:16-24`) | Noise | No uniformity check (needs phantom). |
| Truncation (`backend/agents/geometry.py`) | Artefacts; FOV / scan parameters | Body outside FOV affects dose calculation (external contour). |
| Metal internal/surface/external (`backend/agents/implants.py`) | Artefacts | Detects metal, not artefact severity (streaks). |
| Gas volume (`backend/agents/cavity.py`) | Patient preparation (process) | Pelvis/abdomen only. |
| Patient roll (`backend/agents/alignment.py`) | Patient position and orientation | Central slice only (`alignment.py:100`). |
| Paediatric/adult protocol markers (`backend/agents/integrity.py:24-66`) | Scan parameters vs protocol | Depends on "(Child)"/"(Adult)" naming convention. |
| Couch detection / accessory mask (`backend/utils.py`) | Couch/table | Used for segmentation only; no check that a flat RT couch top was used. |

| Requirement/topic | Status | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Geometric accuracy (scanner, lasers, couch) | Not applicable (per-series tool) | none found | Keep in the scanner QA programme; state this boundary in the intended use. | Low |
| Slice thickness / spacing | Partial | `geometry.py`, `integrity.py:86-100` | Configure `nominal_mm` per protocol; add a check that the scan covers the expected anatomical extent (z-range vs protocol). | Medium |
| HU and density calibration | Partial (sanity only) | `noise.py:28-29`, `fluid.py:40-55` | Document that it is a per-scan sanity check; keep phantom-based CT-number checks in machine QA; consider checking the kVp used against the kVp of the HU-to-density curve in the TPS. | Medium |
| Noise and uniformity | Partial | `noise.py` | Uniformity out of scope (phantom). | Low |
| Artefacts (metal, truncation) | Met (detection) | `implants.py`, `geometry.py` | Validate (section 5). | Medium |
| Patient position and orientation | **Not met** for header checks; Partial for roll | none found for PatientPosition / ImageOrientationPatient; roll in `alignment.py` | Check `PatientPosition` against the expected value per protocol; flag non-axial / oblique `ImageOrientationPatient`; check orientation is consistent across slices. | **High** |
| Couch/table | Not met | none found | Optionally verify the RT flat-top couch is detected. | Low |
| Scan parameters vs protocol (kVp, kernel, reconstruction FOV, pitch) | Not met | none found | Per-protocol expected kVp, convolution kernel and reconstruction diameter in `protocol_overrides`. | Medium |
| DICOM header consistency (Patient ID/Name, Study UID, FrameOfReferenceUID within the series) | **Not met** | none found (engine uses `datasets[0]` only, `engine.py:96`) | REJECT if PatientID, StudyInstanceUID or FrameOfReferenceUID differ across slices; REJECT if the RTSS FrameOfReferenceUID differs from the CT's. | **High** |
| Contrast phase | Partial | `fluid.py:29` (presence of `ContrastBolusAgent` only) | Compare contrast use against protocol expectation; record the phase. | Low |
| Laterality / external markers | Not met (markers only excluded from metal) | `implants.py` `_detect_skin_markers` | Report detected set-up markers (presence and slice) to support isocentre marking review. | Low |

**Gaps that matter most for contouring and dose calculation**:
1. Wrong patient or mixed series (header consistency).
2. Wrong patient position or orientation, since HFS/FFS mix-ups flip laterality.
3. Truncation of the external contour.
4. Incomplete scan extent.
5. HU or kVp inconsistency with the TPS density table.
6. Uncorrected metal artefacts near the target.

### B. AAPM TG-201 (data transfer QA)

| Requirement/topic | Status | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Completeness of received series | Partial | Association-aware stability wait (`listener.py:23-31`, `webApp.yaml:24`: 30 s after the sender disconnects, not the 10 s mentioned in the brief); spacing/duplicate checks | Compare received image count with the number the sender announces (if the modality provides it, e.g. via MPPS or Storage Commitment), or with `ImagesInAcquisition` where present; flag when the series grew after analysis. | High |
| Silent dropping of objects | Not met | Non-CT objects acknowledged with success (`listener.py:123-125`); mismatched-matrix slices dropped without a flag (`engine.py:77-86`) | Report the number of dropped/ignored objects per series as a flag (CONDITIONAL if any CT image was dropped). | High |
| UID handling | Met for path safety | `security.py:16-25`, `listener.py:128-136` | Also verify SOPInstanceUID uniqueness (overwrite on duplicate UID is silent). | Low |
| AE / peer handling | Partial | Optional allow-lists, empty by default (`webApp.yaml:22-23`, `listener.py:50`) | Make allow-lists mandatory in production. | Medium |
| Transfer to the TPS: integrity verification | **Not met** | `dicom_sender.py:57-72` counts C-STORE successes only, then discards the count | Treat any non-success as failure; verify after sending (storage commitment or C-FIND image count); store a transfer record (time, destination, count, result). | **High** |
| Failure handling | **Not met** | Exceptions printed and swallowed (`dicom_sender.py:71-72`); approval returns success (`viewer.py:327-328`) | Propagate failures to the UI and the log; never report "routed" unless all images were acknowledged; add retry and alerting. | **High** |
| What is sent | Partial | Whole folder `*.dcm` (CT + RTSS) (`dicom_sender.py:31`), whole folder copied to `TPS_EXPORT` (`state.py:143`, `viewer.py:326`) | Send exactly the analysed image set (by SOPInstanceUID list from the result); exclude files that arrived after analysis. | High |
| Can a wrong or incomplete series reach the TPS? | **Yes** | Auto-export on ACCEPT (`state.py:141-145`); approve of any status (`viewer.py:315-330`) | Disable auto-export by default; allow approve only for analysed series, with a confirmation for CONDITIONAL and REJECT and a recorded reason. | **Critical** |
| Duplicate transfers | Partial | Re-analysis after late files re-exports (`state.py:141-145`) | Record transfers and avoid re-sending without explicit action. | Medium |
| Transport security | Not met | No TLS on DICOM (`listener.py`, `dicom_sender.py`) | DICOM TLS or a protected network segment (decide with IT). | Medium |

### C. AAPM TG-100 (risk-based QA)

See the FMEA in section 4. **Fail-safe check** (do errors and timeouts default to accept?):
- **Analysis exception:** `engine.analyze_series` raises, nothing is cached, and the series shows PENDING (`state.py:111`, `routers/studies.py`). It does not default to accept, and auto-export does not run. **However**, a PENDING series can still be approved and sent (`viewer.py:315-330`), and the dashboard retries the failed analysis on every refresh.
- **Re-analysis exception:** the old result stays cached and is shown as current (stale).
- **Empty/filtered series:** returns REJECT "No valid CT image slices" (`engine.py:93`). Fail-safe.
- **TotalSegmentator failure:** silent fallback to the rule-based mask, logged as a warning only (`engine.py:188-190`); `metrics.used_totalsegmentator` records which mask was used, but it isn't shown as a flag.
- **Unreliable roll / gas:** `INFO`, which never escalates (by design). The data stay visible in the PDF, but are hidden on screen by default.
- **PDF or log errors:** caught and printed (`state.py:132-139`); the result stays available. No user notification.
- **DICOM send timeout/failure:** swallowed (gap B).
- **Listener stalls:** the association stays open until pynetdicom's network timeout aborts it, then analysis runs on what arrived (partial-series risk).

| Requirement/topic | Status | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Process map and FMEA | Not met (this document is a first draft) | none found | Review section 4 with the physics team and keep it as a controlled document. | High |
| Errors never default to accept | Partial | See list above | Block approval of PENDING/failed series; show stale-result warnings; make send failures visible. | **High** |
| Alert-fatigue control | Partial | INFO status and screen filtering (`status.py`, `ctqa.yaml:98`) | Measure flag rates after validation; keep a target false-positive rate per check. | Medium |

### D. IEC 62304 (proportionate)

Whether IEC 62304 applies formally depends on whether the tool is used as a medical device under MDR Article 5(5). For an in-house research tool it is good practice, not necessarily mandatory: confirm with the regulatory contact.

| Requirement/topic | Status | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Intended use statement | Not met | README describes features only | Write a one-page intended use: users, patient population, what it does not do, that a clinician decides, and machine-QA boundary. | **High** |
| Software requirements and traceability to tests | Partial | Behaviour described in `docs/AGENTS_DETAIL.md`, `docs/CONFIGURATION.md`; 213 tests | Number the requirements (one per check and per workflow step) and map tests to them. | Medium |
| Safety classification rationale | Not met | none found | Classify with rationale. With auto-export to the TPS, software failure could plausibly contribute to harm, so the class is likely B or higher. If auto-export is removed and a clinician reviews every series, document how that external risk control affects the class. Verify against the current edition, since Edition 2 changes classification. | High |
| Version control | Met | git history (~150 commits), PR-based merges | — | — |
| Release process | Not met | no git tags; version strings edited by hand (`webApp.yaml:10`, `pyproject.toml:3`) | Tag releases; release notes; record the deployed version on the server; show it in the PDF. | Medium |
| Configuration management of `ctqa.yaml` | Partial | Validated at startup, unknown keys rejected (`backend/qa_config.py:197`); shipped defaults tested (`backend/test_hardening.py:22`); git history | Define who may change thresholds; require physics review (PR approval) and re-validation (section 5); store `sop.version` and a config hash in every result and PDF. | High |
| Result traceability (software + config version) | Not met | `QAResult` has no version fields (`models.py:19-25`) | Add app version, git commit, `ctqa.yaml` hash and TotalSegmentator version to `metrics` and the PDF. | High |
| SOUP list | Not met | `requirements.txt` (ranges, only `fpdf2` and `scipy` pinned); TotalSegmentator optional and unpinned (2.18.0 on the dev host) with model weights downloaded at first run | Maintain a SOUP list (name, version, purpose, known anomalies, how verified). Pin exact versions (lock file). Archive the TotalSegmentator weights and record their version. | High |
| Unit/integration tests | Met (unit/integration level) | 17 test modules, 213 tests; synthetic phantoms; no network or real data in tests (`conftest.py`) | — | — |
| Validation (clinical) tests | Not met | none found | Section 5. | **High** |
| Not covered by tests | — | Frontend JavaScript (no tests, no browser automation), `cockpit.py`, real DICOM networking end-to-end, TotalSegmentator real inference, Windows deployment | Add a smoke test protocol for each release. | Medium |
| Known-anomaly list | Not met | `TODO.md` has one item | Maintain a known-anomaly list (e.g. roll from the central slice only, gas limited to the inferior half). | Medium |
| Change control | Partial | PRs on GitHub; no documented review/approval requirement | Require physicist approval on PRs touching `backend/agents/`, `ctqa.yaml`, export/approval code. | Medium |

### E. IEC 62366 (usability, proportionate)

| Requirement/topic | Status | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Alert fatigue | Partial | INFO status, per-protocol thresholds, screen hides passing checks (`ctqa.yaml:98`) | Monitor CONDITIONAL rates per check after go-live; summative evaluation with users. | Medium |
| Status vocabulary consistency | Met | Single enum (`backend/status.py`), legacy values normalised | — | — |
| Final decision and its basis clear on screen | Partial | Verdict badge plus attention flags with slice numbers; "No issues detected." plus passed count (`frontend/app.js:42`) | Show unreliable-estimate INFO flags on screen (do not hide INFO with "unreliable" in it), and show which mask method was used. | Medium |
| Decision basis in the PDF | Met | All checks in three sections (`backend/reporter.py:151-153`) | Add software/config version and the clinician decision (who, when, why) to the PDF. | Medium |
| Clinician can see why a series was flagged | Met | Messages carry measured value, limit and slices | — | — |
| Use errors at the decision point | **Not met** | Approve has no confirmation and works for REJECT/PENDING (`frontend/app.js:621-626`); Reject has a confirmation (`app.js:645`) | Disable or guard Approve for non-analysed or REJECT series; require an override reason. | **High** |
| Use specification / user interface evaluation | Not met | none found | Short use specification and formative test with 2–3 users. | Low |

### F. EU MDR 2017/745 Article 5(5) (health-institution exemption)

**Not legal advice.** Whether the tool is a medical device (it is software that processes patient images to support decisions before treatment planning) and whether the exemption is used must be decided by the institution. The conditions below are paraphrased from secondary summaries of the Regulation and MDCG 2023-1: verify against the Regulation text and current MDCG guidance.

| Condition (paraphrased) | Status in repository | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Not transferred to another legal entity; used only within the institution | Cannot assess | Public GitHub repository (code shared publicly) | Clarify with the regulatory contact whether public source code affects the exemption; document that only the institution uses it clinically. | High |
| Appropriate quality management system | Not met | none found | Department or institution QMS covering this tool (document control, change control, training). | High |
| Justification that the target group's needs cannot be met (or not at the appropriate level) by an equivalent device on the market | Not met | none found | Write the justification (market scan of commercial CT-sim QA tools). | High |
| Relevant general safety and performance requirements (Annex I) met | Partial | Some evidence (tests, risk controls in code) | GSPR checklist with evidence references. | High |
| Information on manufacture, modification and use provided to the competent authority on request | Partial | git history | Keep design and change documentation retrievable. | Medium |
| Public declaration (institution name and address, device identification, statement that GSPRs are met, justification where not) | Not met | none found | Draft and publish per institutional procedure. | Medium |
| Documentation of design, manufacture, performance, intended purpose, risk management and validation | Partial | `docs/` (design behaviour), tests; no risk file or validation | Sections 4 and 5 of this review are a starting point. | High |
| Review of clinical use experience and corrective actions | Not met | Problem log exists (`backend/logger.py`) but no review procedure | Periodic review of the problem log and incidents; corrective action records. | Medium |
| No industrial-scale manufacture | Met (by nature) | — | — | — |

### G. GDPR and data protection

Not legal advice; confirm with the DPO. Health data are special-category personal data under the GDPR; a data protection impact assessment is likely expected for this processing (verify).

| Requirement/topic | Status | Evidence | Recommendation | Priority |
|---|---|---|---|---|
| Patient identifiers in logs | Not met (minimisation) | Daily problem log stores patient name and ID (`backend/logger.py:69-70`); CSV export includes both (`logger.py:209-210`) | Store only series UID and a pseudonymous patient key; resolve names on screen from DICOM when needed. | High |
| Identifiers in PDF and filenames | Partial | Name in PDF (`reporter.py:122`), name in download filename (`routers/reports.py:71`) | The PDF is a clinical record (name expected); drop the name from filenames. | Low |
| `qa_result.json` | Partial | Contains patient name (`models.py:21`) | Acceptable alongside the DICOM data it describes; it is deleted with the series. | Low |
| `errors.xlsx` | Partial | git-ignored and untracked; still in git history; no patient names in the rows inspected | Remove from git history if the institution requires it; keep exports in a controlled location. | Low |
| Storage retention and deletion | Partial | Storage folders by `retention_days` (`state.py:248-266`); exports 24 h; **`logs/`, `reports/` and `rejections.log` never purged** | Define retention per data type with the DPO (Hungarian medical-record rules may require keeping some records); implement purge for logs and reports. | High |
| Access control on the API | Partial | IP allow-list, loopback default (`webApp.yaml:33`, `security.py:75-91`) | Authenticated access (reverse proxy with institutional SSO); role for approve/reject. | High |
| CORS | Met | No CORS unless configured (`backend/main.py`, `webApp.yaml:36`) | — | — |
| Binding to 0.0.0.0 | Partial | API default `127.0.0.1` (`webApp.yaml:15`); DICOM listener `0.0.0.0` (`webApp.yaml:19`); site config may open the API | Restrict by firewall and allow-lists; document the network design. | Medium |
| Transport security | Not met | HTTP and DICOM in clear | TLS (reverse proxy for HTTP; DICOM TLS or isolated VLAN). | Medium |
| Audit log of approve/reject with user identity | **Not met** | Reject: timestamp + UID only (`viewer.py:356`); approve: not logged | Append-only audit log: user, time, series, decision, reason, software/config version. | **High** |
| Where data leaves the host | Partial | DICOM routing to `dest.json` destinations (`dicom_sender.py:8-23`); TotalSegmentator posts run metadata (task, version, platform, run counter, anonymous ID; no image or patient data per the installed 2.18.0 source) to its developers' statistics server, enabled by default and enabled on the dev host; model weights downloaded from the internet on first use; this public GitHub repository | Set `send_usage_stats` to false in the TotalSegmentator config on all hosts, or block egress. Pre-install the weights. Never commit site config or patient data; keep the `conftest.py` isolation. | Medium |
| Records of processing / DPIA | Cannot assess | none found | Add RapidCTQA to the institution's processing records; DPIA if required. | Medium |

## 4. FMEA (TG-100 style, first draft)

Scores are suggestions for discussion (1 = best, 10 = worst; D: 10 = undetectable before harm). RPN = S×O×D. Occurrence estimates are qualitative because no validation data exist.

| # | Failure mode | Effect | S | O | D | RPN | Rationale | Existing mitigations | Missing mitigations |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Wrong or incomplete series auto-exported as ACCEPT | Planning on wrong/incomplete data without review | 9 | 4 | 7 | 252 | Auto-export bypasses the clinician; TPS-side checks vary | Verdict rules (`status.py:117-128`); association-aware stability wait (`listener.py:23-31`) | Disable auto-export; require approval; completeness check. |
| 2 | Approval of a REJECT or unanalysed series | Rejected data reach the TPS | 8 | 3 | 6 | 144 | One click, no guard | Approve blocked while ingesting (`viewer.py:320`) | Status guard, confirmation, reason, audit. |
| 3 | DICOM send partially fails, reported as success | Incomplete series in the TPS | 7 | 3 | 6 | 126 | Errors swallowed | C-STORE status counted (`dicom_sender.py:60-68`) | Fail on any non-success, verify after send, surface in UI. |
| 4 | Partial series analysed (sender gap, network abort) | Missing anatomy not flagged; exported if ACCEPT | 8 | 3 | 6 | 144 | No expected-count check | Stability wait; spacing/duplicate checks | Image-count/coverage check; re-analysis-after-growth flag. |
| 5 | Wrong patient / mixed series | Treatment planned on wrong anatomy | 10 | 2 | 7 | 140 | No header consistency checks | none | PatientID/StudyUID/FoR consistency REJECT. |
| 6 | Wrong patient position or orientation (HFS/FFS, oblique) | Laterality or geometry error | 9 | 2 | 6 | 108 | Not checked | Roll check (`alignment.py`) only | PatientPosition vs protocol; orientation check. |
| 7 | False negative: truncation, metal or gas missed | Dose calculation error, contouring issue | 7 | 3 | 5 | 105 | Algorithms not clinically validated | Tests on phantoms; clinician review | Validation (section 5); keep human review mandatory. |
| 8 | False positives / alert fatigue | Real findings ignored | 6 | 5 | 5 | 150 | Historic log dominated by metal and truncation alerts | INFO tier, per-protocol overrides, screen filter | Monitor flag rates; periodic threshold review with validation. |
| 9 | Analysis crash or exception treated as pass | Unchecked series used | 8 | 2 | 4 | 64 | Crash leaves PENDING (fail-safe), but approve still possible | No auto-export without a result (`state.py:111-145`) | Block approve for PENDING; show analysis errors in the UI. |
| 10 | Stale cached result after failed re-analysis or series growth | Decision on outdated result | 6 | 2 | 6 | 72 | Old result remains in cache | Re-analysis on late files; viewer caches invalidated (`state.py`) | Mark result stale when files change after analysis; show analysis time and file count. |
| 11 | `ctqa.yaml` edited without review | Thresholds silently loosened | 7 | 3 | 7 | 147 | Edits are local files on the server | Schema validation (`qa_config.py:197`); git history | Change control, config hash in results, re-validation trigger. |
| 12 | TotalSegmentator mask error (leaks, misses) | Wrong truncation, gas, metal classification | 6 | 3 | 6 | 108 | ML model, version unpinned | Gas candidate cleaning independent of mask (`cavity.py`); body-mask sanity check; rule-based fallback | Pin version and weights; validation per version; show mask method on screen. |
| 13 | Unauthenticated API action (approve/reject/delete) | Unauthorised or untraceable decisions; data loss | 7 | 2 | 8 | 112 | IP allow-list only | Allow-list, CSRF guard, UID validation (`security.py`) | Authentication, roles, audit log. |
| 14 | Reject deletes data irrecoverably by mistake | Rescan needed, delay | 4 | 2 | 3 | 24 | Confirmation exists | Confirm dialog (`app.js:645`) | Soft delete with retention period. |

Highest RPN: 1, 8, 11, 2, 4, 5. Severity ≥ 9 regardless of RPN: 1, 5, 6. TG-100 recommends attention to high-severity modes even with low RPN.

## 5. Validation plan

**Goal**: estimate per-check agreement with expert judgement on clinical planning CTs, for the current version and thresholds, and define acceptance criteria and re-validation triggers.

**Data**
- **Retrospective cohort:** about 300 consecutive planning CT series across the main protocols (H&N, brain, thorax/breast, abdomen, pelvis/prostate, 4DCT), all verdicts, so that negatives are represented. The problem log (`errors.xlsx`, about 215 rows over three exported sheets, possibly overlapping) contains only flagged cases. Its messages use the old vocabulary and thresholds, so it is useful for enrichment and case finding, not as a ground-truth set.
- **Enrichment:** add flagged cases from the problem log so that each check has at least about 30–60 positive cases. For example, with 60 positives and an observed sensitivity of 95%, the lower 95% confidence bound is roughly in the mid-80s. Have a statistician confirm the sample size against the chosen acceptance criteria.
- **Synthetic and phantom cases** for rare, high-severity failures that cannot be found retrospectively: wrong patient mix, HFS/FFS mismatch, partial series, oblique orientation, gross truncation. Use these as a pass/fail test set.

**Ground truth**: two physicists review each case independently, blinded to RapidCTQA output, using a structured form per check (present/absent plus severity: no action / review / reject). Disagreements are resolved by a third reviewer. Record inter-observer agreement (Cohen's kappa) as context for the tool's agreement.

**Metrics per check**
- **Binary findings** (truncation, metal per class, gas above the review threshold, roll above the alert threshold, paediatric mismatch, thickness deviation): sensitivity and specificity for "needs review or reject" against expert judgement, with 95% CIs, plus the false-positive rate per series (alert burden).
- **Quantitative estimates:**
  - Roll: compare with a manual measurement; mean difference and limits of agreement, target within 1° for 95% of cases.
  - Gas volume: compare with manual contouring on a subset; Bland–Altman.
  - Metal volume: compare with manual thresholded contour.
  - Truncation z-extent: compare with manual slice count.
- Verdict-level agreement: confusion matrix of ACCEPT / CONDITIONAL / REJECT against expert decision.

**Acceptance criteria (proposed; set with the physics team before unblinding)**

| Check | Proposed criterion |
|---|---|
| Truncation, anterior/posterior and torso | Sensitivity ≥ 95% (lower CI ≥ 85%) for clinically relevant truncation; no missed anterior/posterior truncation in the phantom set |
| Header consistency, orientation, position (once implemented) | 100% detection on the synthetic test set |
| Metal (internal, ≥ limit) | Sensitivity ≥ 90%; specificity reported |
| Gas (≥ review threshold, pelvis) | Sensitivity ≥ 90%; volume within ±20% or ±10 cc of manual |
| Roll | Agreement within ±1° for 95% of reliable estimates; unreliable rate reported |
| Alert burden | CONDITIONAL rate on series judged acceptable ≤ an agreed target (e.g. 10–15%) |
| Partial series / data transfer | 100% detection of injected incomplete series once completeness checks exist |

**Re-validation triggers**
- **Any change to `ctqa.yaml`** (thresholds, overrides, display): re-run the frozen validation set and compare per-check metrics. Changes that lower sensitivity need physics sign-off.
- **TotalSegmentator version or weights change, or a numpy/scipy/scikit-image/pydicom major version change:**
  - re-run the mask-dependent checks (truncation, gas, metal, roll) on the frozen set;
  - compare against stored reference outputs, with tolerances defined per metric;
  - also re-run the synthetic regression ("golden") comparisons used during development.
- **Algorithm code changes in `backend/agents/`:** full re-run.
- Store the validation set's series UIDs, expert labels and per-version outputs in a controlled location, not in this repository.

**Threshold consistency (prerequisite for validation)**: `ctqa.yaml` values and code defaults are now enforced to match (`backend/test_hardening.py:22`); no disagreement found between `ctqa.yaml` and the code at this commit. Several parameters remain **hardcoded and are not in `ctqa.yaml`**, so validation should record them as fixed design values:
- tissue threshold −300 HU for rule-based segmentation (`engine.py:141`)
- accessory threshold −500 HU with the TotalSegmentator mask (`engine.py:194`)
- fluid body mask −500 HU (`fluid.py:16`)
- air estimate from the 1st percentile above −1500 HU (`noise.py:28-29`)
- 40×40 px centre noise ROI (`noise.py:33`)
- gas limited to the inferior half of the slices (`cavity.py:110`)
- lateral truncation sectors 315°–45° and 135°–225° (`geometry.py:54`)
- roll from the central slice only (`alignment.py:100`)

Thresholds and status rules changed in PRs #46–#48 (for example gas, metal, roll and slice thickness). Results in `errors.xlsx` therefore cannot be compared with the current version without re-running the analysis.

## 6. Questions for the physicist / owner

1. Is auto-export of ACCEPT series to the TPS intended in clinical use? If yes, what independent check happens in the TPS before contouring?
2. Who may approve and reject? Is a named-user audit trail required by departmental policy?
3. Does the department treat RapidCTQA as a medical device under MDR Article 5(5), or as a non-device research/QA tool? Who is the regulatory contact?
4. Is there a departmental or institutional QMS the tool should be placed under (document control, change control, training)?
5. What are the expected `PatientPosition`, kVp, kernel, reconstruction FOV and nominal slice thickness per protocol (needed for the missing checks and `protocol_overrides`)?
6. How does the TPS handle duplicate or partial series received over DICOM? Is storage commitment or query available on the TPS for post-transfer verification?
7. What retention periods apply to the QA log, PDFs and `qa_result.json` under Hungarian medical-record rules and institutional policy? Has the DPO assessed this processing (DPIA, processing records)?
8. Is outbound internet access from the server allowed (TotalSegmentator statistics and weight downloads)? Should it be blocked?
9. Which TotalSegmentator version and weights are in clinical use on the server, and is GPU or CPU used?
10. Is the network design (VLAN, firewall, who can reach the dashboard and the DICOM port) documented?
11. Is the scanner QA programme (TG-66 / TG-66U1 equipment tests) in place and documented, so this tool's scope can be stated as complementary?
12. Who reviews the problem log, how often, and what happens to findings (corrective actions)?

*Sources consulted for edition/scope checks: AAPM report pages for TG-66 (Report 83), TG-66U1 (Report 83.B, Med Phys 2026), TG-201 (JACMP 2011 rapid communication; Med Phys 2021 full report); MDCG 2023-1 (European Commission, January 2023) and secondary summaries of MDR Article 5(5); published status summaries of IEC 62304 Edition 2; TotalSegmentator documentation and installed package source (`totalsegmentator/config.py`, v2.18.0).*
