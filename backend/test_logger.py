import os
import json
import pytest
from backend.models import QAResult, QAFlag
from backend.logger import (
    log_qa_result,
    get_all_logs,
    query_logs,
    export_logs_csv,
    get_daily_log_path,
    LOGS_DIR,
)


def test_log_qa_result_and_query(tmp_path):
    result = QAResult(
        series_uid="1.2.3.4.5.6.7890",
        patient_name="DOE^JOHN",
        protocol="Pelvis_Std",
        status="REJECT",
        metrics={"gas_volume_cc": 55.2, "background_air_sd": 12.5},
        flags=[
            QAFlag(name="CavityScout", status="REJECT", message="Excessive Bowel Gas detected (55.2 cc)"),
            QAFlag(name="GeometryGuardian", status="ACCEPT", message="FOV bounds ok"),
        ],
    )

    log_rec = log_qa_result(result, patient_id="PID12345", timestamp="2025-05-10T14:30:00")

    assert log_rec["series_uid"] == "1.2.3.4.5.6.7890"
    assert log_rec["patient_name"] == "DOE^JOHN"
    assert log_rec["patient_id"] == "PID12345"
    assert log_rec["protocol"] == "Pelvis_Std"
    assert log_rec["status"] == "REJECT"
    assert len(log_rec["issues"]) == 1
    assert "CavityScout" in log_rec["issues"][0]

    # Verify daily file on disk
    expected_path = get_daily_log_path("2025-05-10")
    assert os.path.exists(expected_path)
    with open(expected_path, "r", encoding="utf-8") as f:
        daily_data = json.load(f)
        assert len(daily_data) >= 1
        found = [d for d in daily_data if d["series_uid"] == "1.2.3.4.5.6.7890"]
        assert len(found) == 1

    # Query tests
    logs_date = query_logs(date_str="2025-05-10")
    assert len(logs_date) >= 1

    logs_status = query_logs(status="REJECT")
    assert any(l["series_uid"] == "1.2.3.4.5.6.7890" for l in logs_status)

    logs_issue = query_logs(issue_type="CavityScout")
    assert any(l["series_uid"] == "1.2.3.4.5.6.7890" for l in logs_issue)

    logs_search = query_logs(search="DOE^JOHN")
    assert any(l["series_uid"] == "1.2.3.4.5.6.7890" for l in logs_search)

    # Test CSV export
    csv_str = export_logs_csv([log_rec])
    assert "1.2.3.4.5.6.7890" in csv_str
    assert "DOE^JOHN" in csv_str
    assert "CavityScout" in csv_str
