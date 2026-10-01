"""Tests for config validation, status normalisation, listener input checks and cleanup scope."""
import os
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pydicom.dataset import Dataset, FileMetaDataset

from backend import settings, state
from backend.listener import DicomListener, STATUS_CANNOT_UNDERSTAND, STATUS_REFUSED, STATUS_SUCCESS
from backend.logger import query_logs
from backend.models import QAFlag, QAResult
from backend.qa_config import QAConfig, load_qa_config
from backend.security import safe_child_path, series_dir
from backend.settings import StorageUnavailableError, normalise_storage_path
from backend.status import QAStatus, normalize_status, series_verdict


# --- ctqa.yaml ---------------------------------------------------------------

def test_shipped_ctqa_yaml_loads_and_matches_defaults():
    # Defaults in qa_config.py and the shipped YAML must not drift apart
    assert load_qa_config(settings.QA_CONFIG_PATH).thresholds == QAConfig().thresholds


@pytest.mark.parametrize("yaml_text", [
    "rules:\n  geometry: []\n",                          # dead rule DSL
    "thresholds:\n  gas:\n    conditonal_cc: 20\n",      # typo
    "thresholds:\n  gas:\n    conditional_cc: 80\n",     # above reject_cc
])
def test_invalid_ctqa_yaml_fails_loudly(tmp_path, yaml_text):
    path = tmp_path / "ctqa.yaml"
    path.write_text(yaml_text)
    with pytest.raises(ValueError):
        load_qa_config(str(path))


def test_thresholds_from_yaml_drive_the_rules(tmp_path):
    from backend.engine import QAEngine
    path = tmp_path / "ctqa.yaml"
    path.write_text("thresholds:\n  gas:\n    conditional_cc: 5\n    reject_cc: 8\n    leak_cc: 100\n")
    engine = QAEngine(str(path))
    flags = engine._evaluate_rules({
        "is_pelvis_or_abdomen_scan": True, "gas_volume_cc": 10.0, "gas_slices": [2, 3],
        "slice_spacing_var": 0.0, "monotonic_z": True, "gantry_tilt": 0.0, "duplicate_slices": False,
        "background_air_sd": 5.0, "air_hu_estimate": -1000.0, "rescale_slope": 1.0,
        "radon_status": "SKIPPED", "pediatric_mismatch": False, "slice_count": 10, "slice_thickness": 2.0,
    })
    gas = [f for f in flags if f.name == "CavityScout"]
    assert gas[0].status == QAStatus.REJECT
    assert "Slices 2-3" in gas[0].message


# --- status vocabulary ------------------------------------------------------

@pytest.mark.parametrize("legacy,canonical", [
    ("PASS", "ACCEPT"), ("PASS_WITH_WARNING", "CONDITIONAL"), ("FAIL_CRITICAL", "REJECT"),
    ("accept", "ACCEPT"), ("SKIPPED", "SKIPPED"),
])
def test_legacy_status_values_are_normalised(legacy, canonical):
    assert normalize_status(legacy) == canonical
    result = QAResult(series_uid="1.2", status=legacy, metrics={},
                      flags=[QAFlag(name="X", status=legacy)])
    assert result.status == canonical and result.flags[0].status == canonical
    assert f'"status":"{canonical}"' in result.model_dump_json()


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError):
        QAFlag(name="X", status="MAYBE")


def test_series_verdict_ignores_skipped():
    assert series_verdict(["SKIPPED", "ACCEPT"]) == QAStatus.ACCEPT
    assert series_verdict(["SKIPPED", "CONDITIONAL"]) == QAStatus.CONDITIONAL
    assert series_verdict(["CONDITIONAL", "REJECT"]) == QAStatus.REJECT
    assert series_verdict([]) == QAStatus.ACCEPT


def test_log_filter_matches_legacy_records():
    from backend.logger import log_qa_result
    log_qa_result(QAResult(series_uid="1.9.9.1", status="FAIL_CRITICAL", metrics={}, flags=[]))
    assert any(r["series_uid"] == "1.9.9.1" for r in query_logs(status="REJECT"))
    assert any(r["series_uid"] == "1.9.9.1" for r in query_logs(status="FAIL_CRITICAL"))
    assert not any(r["series_uid"] == "1.9.9.1" for r in query_logs(status="ACCEPT"))


# --- paths -------------------------------------------------------------------

@pytest.mark.parametrize("uid", ["..", "../x", "1.2/../../etc", "1..2", "", "1.2.", "a.b"])
def test_series_dir_refuses_non_uids(tmp_path, uid):
    with pytest.raises(ValueError):
        series_dir(str(tmp_path), uid)


def test_safe_child_path_refuses_escape(tmp_path):
    with pytest.raises(ValueError):
        safe_child_path(str(tmp_path), "../outside")
    assert safe_child_path(str(tmp_path), "1.2.3").endswith("1.2.3")


def test_storage_alias_uses_first_reachable_candidate(tmp_path):
    mount = tmp_path / "mnt"
    mount.mkdir()
    aliases = [{"from": "\\\\srv\\DICOM", "to": ["\\\\srv\\DICOM", str(mount)]}]
    assert normalise_storage_path("\\\\srv\\DICOM\\rapidqa", aliases) == os.path.join(str(mount), "rapidqa")
    assert normalise_storage_path("data/rtct", aliases) == "data/rtct"
    with pytest.raises(StorageUnavailableError):
        normalise_storage_path("\\\\srv\\DICOM\\rapidqa", [{"from": "\\\\srv\\DICOM", "to": [str(tmp_path / "nope")]}])


# --- startup cleanup -------------------------------------------------------------

def test_cleanup_only_removes_old_uid_folders(tmp_path):
    storage, export = tmp_path / "storage", tmp_path / "export"
    old_series, new_series, unrelated = storage / "1.2.3.4", storage / "1.2.3.5", storage / "Department Share"
    for d in (old_series, new_series, unrelated, export):
        d.mkdir(parents=True)
    old = (datetime.now() - timedelta(days=3)).timestamp()
    for d in (old_series, unrelated):
        os.utime(d, (old, old))

    with patch.object(settings, "STORAGE_DIR", str(storage)), patch.object(settings, "EXPORT_DIR", str(export)):
        state.cleanup_old_directories()

    assert not old_series.exists()
    assert new_series.exists()
    assert unrelated.exists()


def test_cleanup_disabled_with_zero_retention(tmp_path):
    old_series = tmp_path / "1.2.3.4"
    old_series.mkdir()
    old = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(old_series, (old, old))
    with patch.object(settings, "STORAGE_DIR", str(tmp_path)), \
         patch.object(settings, "EXPORT_DIR", str(tmp_path / "none")), \
         patch.object(settings, "STORAGE_RETENTION_DAYS", 0):
        state.cleanup_old_directories()
    assert old_series.exists()


# --- DICOM listener -------------------------------------------------------------

def _ct_event(series_uid="1.2.3", sop_uid="1.2.3.4", assoc=None, peer="10.0.0.5"):
    ds = Dataset()
    ds.Modality = "CT"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 12, 11
    ds.PixelRepresentation, ds.SamplesPerPixel = 0, 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x00" * 8
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = sop_uid
    fm.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    event = MagicMock()
    event.dataset, event.file_meta = ds, fm
    event.assoc = assoc or MagicMock()
    event.assoc.requestor.address = peer
    return event


@pytest.mark.parametrize("series_uid,sop_uid", [("../../evil", "1.2"), ("1.2.3", "../x"), ("1.2.3", "1.2.3/..")])
def test_listener_refuses_path_like_uids(tmp_path, series_uid, sop_uid):
    listener = DicomListener(str(tmp_path / "store"), callback=MagicMock())
    status = listener._handle_store(_ct_event(series_uid, sop_uid))
    assert status == STATUS_CANNOT_UNDERSTAND
    assert not (tmp_path / "evil").exists()
    assert listener.series_tracker == {}


def test_listener_refuses_peer_outside_allow_list(tmp_path):
    listener = DicomListener(str(tmp_path), callback=MagicMock(), allowed_peers=["192.168.1.0/24"])
    assert listener._handle_store(_ct_event(peer="10.0.0.5")) == STATUS_REFUSED
    assert listener._handle_store(_ct_event(peer="192.168.1.20")) == STATUS_SUCCESS
    assert os.path.exists(tmp_path / "1.2.3" / "1.2.3.4.dcm")


def test_listener_waits_for_open_association(tmp_path):
    callback = MagicMock()
    listener = DicomListener(str(tmp_path), callback=callback, stability_seconds=0.05)
    assoc = MagicMock()
    listener._handle_store(_ct_event(assoc=assoc))

    time.sleep(0.3)  # many stability windows pass while the sender is still connected
    callback.assert_not_called()
    assert listener.is_ingesting("1.2.3")

    closed = MagicMock()
    closed.assoc = assoc
    listener._handle_assoc_closed(closed)
    time.sleep(0.3)
    callback.assert_called_once_with("1.2.3")
    assert not listener.is_ingesting("1.2.3")
