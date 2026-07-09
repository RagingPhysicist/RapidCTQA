import os
import shutil
import pytest
from unittest.mock import patch, MagicMock

# Import the objects we need to test
from backend.main import reject_series, results_cache, ct_files_cache, results_cache_lock

@pytest.fixture
def setup_dummy_data(tmp_path):
    storage_dir = tmp_path / "data"
    export_dir = tmp_path / "export"
    reports_dir = tmp_path / "reports"

    storage_dir.mkdir()
    export_dir.mkdir()
    reports_dir.mkdir()

    series_uid = "TEST_UID_123"

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

    # Patch the directory constants in backend.main
    with patch("backend.main.STORAGE_DIR", data["storage_dir"]), \
         patch("backend.main.EXPORT_DIR", data["export_dir"]), \
         patch("backend.main.REPORTS_DIR", data["reports_dir"]), \
         patch("backend.main.ROOT_DIR", data["storage_dir"]): # for rejections.log

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
