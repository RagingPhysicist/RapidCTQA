import os
import shutil
import pytest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from backend.main import app, reject_series, results_cache, ct_files_cache, results_cache_lock
from backend.security import CSRF_HEADER

@pytest.fixture
def setup_dummy_data(tmp_path):
    storage_dir = tmp_path / "data"
    export_dir = tmp_path / "export"
    reports_dir = tmp_path / "reports"

    storage_dir.mkdir()
    export_dir.mkdir()
    reports_dir.mkdir()

    series_uid = "1.2.826.0.1.3680043.2.1125.123"

    # Create files
    series_storage = storage_dir / series_uid
    series_storage.mkdir()
    (series_storage / "image.dcm").write_text("dummy dicom")
    (series_storage / "qa_result.json").write_text("{}")

    series_export = export_dir / series_uid
    series_export.mkdir()
    (series_export / "image.dcm").write_text("dummy dicom")

    pdf_report = reports_dir / f"QA_Report_{series_uid}.pdf"
    pdf_report.write_text("dummy pdf")

    # Populate caches
    with results_cache_lock:
        results_cache[series_uid] = MagicMock()
        ct_files_cache[series_uid] = ["path/to/file"]

    return {
        "series_uid": series_uid,
        "storage_dir": str(storage_dir),
        "export_dir": str(export_dir),
        "reports_dir": str(reports_dir),
        "pdf_path": str(pdf_report),
        "series_storage": str(series_storage),
        "series_export": str(series_export)
    }

@pytest.mark.asyncio
async def test_reject_series_cleanup(setup_dummy_data):
    data = setup_dummy_data
    series_uid = data["series_uid"]

    # Patch the directory settings
    with patch("backend.settings.STORAGE_DIR", data["storage_dir"]), \
         patch("backend.settings.EXPORT_DIR", data["export_dir"]), \
         patch("backend.settings.REPORTS_DIR", data["reports_dir"]), \
         patch("backend.settings.ROOT_DIR", data["storage_dir"]): # for rejections.log

        # Call the function
        response = await reject_series(series_uid)

        # Verify response
        assert response["message"] == f"{series_uid} rejected"

        # Verify files are deleted
        assert not os.path.exists(data["series_storage"])
        assert not os.path.exists(data["series_export"])
        assert not os.path.exists(data["pdf_path"])

        # Verify caches are cleared
        with results_cache_lock:
            assert series_uid not in results_cache
            assert series_uid not in ct_files_cache

        # Verify log exists
        assert os.path.exists(os.path.join(data["storage_dir"], "rejections.log"))


@pytest.fixture
def client():
    # TestClient connects as host "testclient"; allow it like a local browser
    with patch("backend.security.ip_allowed", return_value=True):
        yield TestClient(app)


@pytest.mark.parametrize("bad_uid", ["..", "1.2..3", "1.2.3%2F..%2F..", "abc", "1." * 40])
def test_reject_refuses_invalid_series_uid(client, tmp_path, bad_uid):
    victim = tmp_path / "victim"
    victim.mkdir()
    with patch("backend.settings.STORAGE_DIR", str(tmp_path / "storage")):
        response = client.post(f"/api/viewer/{bad_uid}/reject", headers={CSRF_HEADER: "1"})
    assert response.status_code in (400, 404)
    assert victim.exists()


def test_post_without_csrf_header_is_refused(client, setup_dummy_data):
    data = setup_dummy_data
    with patch("backend.settings.STORAGE_DIR", data["storage_dir"]):
        response = client.post(f"/api/viewer/{data['series_uid']}/reject")
    assert response.status_code == 403
    assert os.path.exists(data["series_storage"])


def test_cross_origin_post_is_refused(client, setup_dummy_data):
    data = setup_dummy_data
    with patch("backend.settings.STORAGE_DIR", data["storage_dir"]):
        response = client.post(
            f"/api/viewer/{data['series_uid']}/reject",
            headers={CSRF_HEADER: "1", "Origin": "https://evil.example"},
        )
    assert response.status_code == 403
    assert os.path.exists(data["series_storage"])


def test_client_outside_allow_list_is_refused():
    client = TestClient(app)  # host "testclient" is not an IP in allowed_clients
    assert client.get("/api/status").status_code == 403
