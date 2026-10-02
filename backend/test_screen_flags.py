"""Passing checks are listed in the PDF only, not on screen or in the problem log."""
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend import reporter, settings, state
from backend.logger import get_all_logs, log_qa_result
from backend.main import app
from backend.models import QAFlag, QAResult
from backend.qa_config import QAConfig
from backend.status import QAStatus, is_attention_status, screen_flags

UID = "1.2.826.0.1.3680043.10.1"


def make_result(uid=UID, attention=True):
    flags = [
        QAFlag(name="GeometryGuardian", status="ACCEPT", message="FOV: no truncation detected"),
        QAFlag(name="ImplantAuditor", status="INFO", message="SURFACE_METAL 0.40 cc below limit"),
        QAFlag(name="Legacy", status="PASS", message="legacy pass value"),
        QAFlag(name="FluidPhysicist", status="SKIPPED", message="IV Contrast detected"),
    ]
    if attention:
        flags.append(QAFlag(name="AlignmentAuditor", status="CONDITIONAL", message="ROLL_ALERT rotation"))
        flags.append(QAFlag(name="Integrity", status="FAIL_CRITICAL", message="Slice thickness too large"))
    from backend.status import series_verdict
    return QAResult(series_uid=uid, patient_name="Phantom", protocol="RTP Pelvis",
                    status=series_verdict(f.status for f in flags), metrics={"slice_count": 10}, flags=flags)


# --- helpers & config -------------------------------------------------------------

def test_screen_flags_keeps_attention_and_skipped():
    flags = make_result().flags
    assert [f.status for f in screen_flags(flags)] == ["SKIPPED", "CONDITIONAL", "REJECT"]
    assert [is_attention_status(s) for s in ("PASS", "ACCEPT", "INFO", "SKIPPED", "PASS_WITH_WARNING", "FAIL_CRITICAL")] \
        == [False, False, False, False, True, True]


def test_hidden_statuses_are_configurable_but_never_hide_attention():
    cfg = QAConfig.model_validate({"display": {"screen_hidden_statuses": ["ACCEPT", "SKIPPED"]}})
    assert [f.status for f in screen_flags(make_result().flags, cfg.display.hidden_statuses)] \
        == ["INFO", "CONDITIONAL", "REJECT"]
    with pytest.raises(ValueError):
        QAConfig.model_validate({"display": {"screen_hidden_statuses": ["ACCEPT", "CONDITIONAL"]}})


# --- API ----------------------------------------------------------------------------

@pytest.fixture
def client():
    with patch("backend.security.ip_allowed", return_value=True):
        yield TestClient(app)


@pytest.fixture
def cached(request):
    result = request.param if hasattr(request, "param") else make_result()
    with patch.dict(state.results_cache, {result.series_uid: result}, clear=True):
        yield result


def test_study_detail_returns_only_screen_flags(client, cached):
    data = client.get(f"/api/studies/{UID}").json()
    assert [f["status"] for f in data["flags"]] == ["SKIPPED", "CONDITIONAL", "REJECT"]
    assert data["passed_checks"] == 3
    assert data["show_passed_summary"] is True
    assert data["status"] == "REJECT"
    # The cached / stored result is untouched
    assert len(state.results_cache[UID].flags) == 6


@pytest.mark.parametrize("cached", [make_result(attention=False)], indirect=True)
def test_all_pass_series_has_no_screen_findings_and_a_passed_count(client, cached):
    data = client.get(f"/api/studies/{UID}").json()
    assert [f["status"] for f in data["flags"]] == ["SKIPPED"]
    assert data["passed_checks"] == 3 and data["status"] == "ACCEPT"
    # The dashboard renders this as "No issues detected." + "3 checks passed - full list in PDF"
    app_js = open(os.path.join(settings.FRONTEND_DIR, "app.js"), encoding="utf-8").read()
    assert "No issues detected." in app_js and "passed - full list in PDF" in app_js


def test_viewer_info_returns_only_screen_flags(client, cached):
    with patch.object(state, "get_series_ct_files", return_value=["slice.dcm"]):
        data = client.get(f"/api/viewer/{UID}/info").json()
    assert [f["status"] for f in data["flags"]] == ["SKIPPED", "CONDITIONAL", "REJECT"]
    assert data["passed_checks"] == 3


class _PlainPDF(reporter.QAPDFReport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_compression(False)  # readable text in the PDF bytes


def _pdf_text(path):
    return open(path, "rb").read().decode("latin-1")


def test_pdf_lists_all_checks_in_sections(tmp_path):
    path = str(tmp_path / "r.pdf")
    with patch.object(reporter, "QAPDFReport", _PlainPDF):
        reporter.generate_pdf_report(make_result(), path)
    text = _pdf_text(path)
    for heading in ("Findings requiring attention", "Passed checks", "Skipped checks"):
        assert heading in text
    for message in ("FOV: no truncation detected", "SURFACE_METAL 0.40 cc below limit",
                    "legacy pass value", "IV Contrast detected", "ROLL_ALERT rotation"):
        assert message in text
    assert text.index("ROLL_ALERT rotation") < text.index("Passed checks") < text.index("FOV: no truncation") \
        < text.index("Skipped checks") < text.index("IV Contrast detected")


def test_pdf_endpoint_uses_full_result(client, cached, tmp_path):
    with patch.object(settings, "REPORTS_DIR", str(tmp_path)), patch.object(reporter, "QAPDFReport", _PlainPDF):
        response = client.get(f"/api/reports/{UID}/pdf")
    assert response.status_code == 200
    text = response.content.decode("latin-1")
    assert "FOV: no truncation detected" in text and "SURFACE_METAL 0.40 cc below limit" in text


# --- problem log -----------------------------------------------------------------------

def test_logger_ignores_passing_flags():
    uid = "1.2.826.0.1.3680043.10.2"
    record = log_qa_result(make_result(uid))
    assert record["issues"] == ["AlignmentAuditor: ROLL_ALERT rotation", "Integrity: Slice thickness too large"]
    assert len(record["flags"]) == 6  # full flag list kept

    assert log_qa_result(make_result(uid, attention=False)) is None
    assert not any(r["series_uid"] == uid for r in get_all_logs())


def test_issue_filter_only_matches_attention_flags():
    from backend.logger import query_logs
    uid = "1.2.826.0.1.3680043.10.3"
    log_qa_result(make_result(uid))
    hits = lambda issue: any(r["series_uid"] == uid for r in query_logs(issue_type=issue))
    assert hits("AlignmentAuditor")
    assert not hits("ImplantAuditor")  # INFO only: present in the record, not an issue
