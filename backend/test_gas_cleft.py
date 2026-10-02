"""CavityScout must not count air between the thighs / in the gluteal cleft as
gas, whichever body mask (TotalSegmentator or rule-based) is used."""
import os
from unittest.mock import MagicMock

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileMetaDataset

from backend import settings
from backend.agents import cavity
from backend.agents.base import SeriesContext
from backend.engine import QAEngine

NZ, NY, NX = 40, 300, 320
DZ, DXY = 2.5, 1.0
VOXEL_CC = DZ * DXY * DXY / 1000.0
SKIN_POSTERIOR_Y = 250
THRESHOLDS = QAEngine(settings.QA_CONFIG_PATH).thresholds_for("RTP Pelvis")

zz, yy, xx = np.ogrid[:NZ, :NY, :NX]
BODY = np.broadcast_to(((xx - 160) / 140.0) ** 2 + ((yy - 150) / 100.0) ** 2 <= 1, (NZ, NY, NX))
COUCH = np.broadcast_to((yy >= 262) & (yy < 270) & (xx >= 10) & (xx < 310), (NZ, NY, NX))


def pelvis_phantom():
    hu = np.full((NZ, NY, NX), -1000.0)
    hu[BODY] = 30.0
    hu[COUCH] = 0.0
    return hu


def add_cleft_slit(hu):
    """2 px wide air slit, enclosed in-plane on every slice, running the full z range."""
    hu[:, 200:235, 159:161] = -1000.0
    return hu


def sphere(radius_mm, center_slice, cy, cx):
    return ((zz - center_slice) * DZ) ** 2 + ((yy - cy) * DXY) ** 2 + ((xx - cx) * DXY) ** 2 <= radius_mm ** 2


RECTAL_RADIUS_MM = (3 * 20000 / (4 * np.pi)) ** (1 / 3)  # 20 cc sphere


def add_rectal_bubble(hu):
    hu[sphere(RECTAL_RADIUS_MM, 10, 150, 160)] = -1000.0  # > 80 mm from the skin
    return hu


def gas_metrics(hu, body_mask, accessory=None):
    ds = Dataset()
    ds.ProtocolName = "RTP Pelvis"
    ds.SliceThickness = DZ
    ctx = SeriesContext(
        datasets=[ds], hu_volume=hu, protocol="RTP Pelvis", pixel_spacing=(DXY, DXY),
        voxel_vol_cc=VOXEL_CC, interior_mask=body_mask,
        accessory_table_mask=COUCH.copy() if accessory is None else accessory,
        empty_slices=[], thresholds=THRESHOLDS, slice_spacing_mm=DZ)
    return cavity.compute(ctx)


def ts_like_mask():
    # TotalSegmentator-style body: the filled outer contour, including any air inside it
    return BODY.copy()


def test_a_enclosed_cleft_slit_is_not_gas():
    m = gas_metrics(add_cleft_slit(pelvis_phantom()), ts_like_mask())
    assert m["gas_volume_cc"] == pytest.approx(0.0, abs=0.1)
    assert m["gas_rejected_cc"] > 1.0
    assert {c["reason"] for c in m["gas_components"]} == {"sheet_like"}
    assert m["gas_slices"] == []


def test_b_rectal_bubble_measured_next_to_cleft_slit():
    hu = add_rectal_bubble(add_cleft_slit(pelvis_phantom()))
    m = gas_metrics(hu, ts_like_mask())
    assert m["gas_volume_cc"] == pytest.approx(20.0, rel=0.10)
    kept = [c for c in m["gas_components"] if c["reason"] == "kept"]
    assert len(kept) == 1 and kept[0]["depth_mm"] > 25
    # Reported slices are those of the bubble only, not the full-length slit
    bubble_slices = sorted({int(z) + 1 for z in np.nonzero(sphere(RECTAL_RADIUS_MM, 10, 150, 160))[0]})
    assert m["gas_slices"] == bubble_slices


def test_c_inter_thigh_pocket_closed_on_some_slices_is_exterior():
    hu = pelvis_phantom()
    hu[:, 40:90, 150:170] = -1000.0          # anterior air channel, open to the outside ...
    hu[5:15, 50:56, 150:170] = 30.0          # ... but sealed by skin on slices 6-15
    m = gas_metrics(hu, ts_like_mask())
    assert m["gas_volume_cc"] == pytest.approx(0.0, abs=0.1)
    assert any(c["reason"] == "exterior_connected" and c["volume_cc"] > 5 for c in m["gas_components"])


def test_c2_air_reaching_last_slice_and_couch_is_exterior():
    # Posterior air pocket enclosed in-plane, but running out of the scan at the
    # last slice and touching the couch region there
    hu = pelvis_phantom()
    hu[25:, 215:262, 120:200] = -1000.0
    body = ts_like_mask()
    body[25:, 215:262, 120:200] = True
    m = gas_metrics(hu, body)
    assert all(c["reason"] != "kept" for c in m["gas_components"])


def test_d_bubble_at_couch_exclusion_zone_unchanged():
    cutoff_y = SKIN_POSTERIOR_Y - int(THRESHOLDS.gas.couch_exclusion_mm / DXY)
    bubble = sphere(8.0, 10, cutoff_y, 160)
    hu = pelvis_phantom()
    hu[bubble] = -1000.0
    m = gas_metrics(hu, ts_like_mask())
    # As before: only the part above the couch-interface cut-off counts
    expected = np.count_nonzero(bubble[:, :cutoff_y, :]) * VOXEL_CC
    assert m["gas_volume_cc"] == pytest.approx(expected, rel=1e-6)


def test_cleft_bay_sealed_by_partial_volume_is_rejected():
    # A posterior bay open to the outside through partial-volume voxels
    # (-400 HU: not air, not skin) holds air that is not exterior-connected
    hu = pelvis_phantom()
    hu[:, 180:250, 150:170] = -1000.0
    hu[:, 245:252, 150:170] = -400.0
    m = gas_metrics(hu, ts_like_mask())
    reasons = {c["reason"] for c in m["gas_components"]}
    assert "kept" not in reasons and reasons & {"cleft", "too_shallow"}


def test_components_report_diagnostics():
    m = gas_metrics(add_rectal_bubble(add_cleft_slit(pelvis_phantom())), ts_like_mask())
    for comp in m["gas_components"]:
        assert set(comp) == {"volume_cc", "centroid", "depth_mm", "thickness_mm", "reason"}
        assert set(comp["centroid"]) == {"slice", "y", "x"}
    assert m["gas_component_count"] == 2


# --- (e) through the engine: TotalSegmentator mask vs rule-based mask ---------------

def _write_series(directory, hu):
    paths = []
    for i, sl in enumerate(hu):
        ds = Dataset()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
        ds.SOPInstanceUID = f"1.2.3.9.{i + 1}"
        ds.SeriesInstanceUID = "1.2.3.9"
        ds.Modality = "CT"
        ds.PatientName = "Phantom"
        ds.ProtocolName = "RTP Pelvis"
        ds.Rows, ds.Columns = sl.shape
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 12, 11
        ds.PixelRepresentation, ds.SamplesPerPixel = 0, 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelSpacing = [DXY, DXY]
        ds.SliceThickness = DZ
        ds.ImagePositionPatient = [0.0, 0.0, float(i * DZ)]
        ds.RescaleSlope, ds.RescaleIntercept = 1.0, -1024.0
        ds.PixelData = np.clip(sl + 1024, 0, 4095).astype(np.uint16).tobytes()
        fm = FileMetaDataset()
        fm.TransferSyntaxUID = "1.2.840.10008.1.2.1"
        fm.MediaStorageSOPClassUID = ds.SOPClassUID
        fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta = fm
        p = os.path.join(directory, f"s{i:03d}.dcm")
        ds.save_as(p, enforce_file_format=True)
        paths.append(p)
    return paths


def test_e_totalsegmentator_mask_with_cleft_air_matches_rule_based(tmp_path):
    hu = add_rectal_bubble(add_cleft_slit(pelvis_phantom()))
    paths = _write_series(str(tmp_path), hu)

    service = MagicMock()
    service.is_available = True
    service.load_body_mask.return_value = ts_like_mask()  # includes the cleft slit
    ts = QAEngine(settings.QA_CONFIG_PATH, segmentation_service=service).analyze_series(paths)
    rule = QAEngine(settings.QA_CONFIG_PATH).analyze_series(paths)

    assert ts.metrics["used_totalsegmentator"] is True
    assert rule.metrics["used_totalsegmentator"] is False
    for metrics in (ts.metrics, rule.metrics):
        assert metrics["gas_volume_cc"] == pytest.approx(20.0, rel=0.10)
    assert ts.metrics["gas_volume_cc"] == pytest.approx(rule.metrics["gas_volume_cc"], rel=0.02)
    assert ts.metrics["gas_slices"] == rule.metrics["gas_slices"]
