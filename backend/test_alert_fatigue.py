"""Tests for the alert-fatigue changes: INFO status, one flag per check,
per-protocol thresholds, metal / gas / slice-thickness tiers, truncation
classes, the mirror-symmetry roll estimator and the 4DCT metal policy."""
import os
from unittest.mock import patch

import numpy as np
import pytest
import scipy.ndimage as ndi
from pydicom.dataset import Dataset, FileMetaDataset

from backend import settings
from backend.agents.alignment import estimate_roll
from backend.engine import QAEngine
from backend.logger import get_all_logs, log_qa_result
from backend.models import QAFlag, QAResult
from backend.qa_config import AlignmentThresholds, QAConfig, load_qa_config
from backend.status import QAStatus, is_actionable, series_verdict, severity

ENGINE = QAEngine(settings.QA_CONFIG_PATH)

# Metrics that make every non-tested check pass, for rule-level tests
BASE_METRICS = {
    "protocol": "RTP Test", "slice_spacing_var": 0.0, "monotonic_z": True, "duplicate_slices": False,
    "gantry_tilt": 0.0, "background_air_sd": 5.0, "air_hu_estimate": -1000.0, "fluid_pixels_found": True,
    "fluid_median_hu": 20.0, "rescale_slope": 1.0, "is_pelvis_or_abdomen_scan": False, "gas_volume_cc": 0.0,
    "metal_internal_cc": 0.0, "metal_surface_cc": 0.0, "metal_external_cc": 0.0, "roll_deg": 0.0,
    "roll_correlation": 0.99, "roll_reliable": True, "pediatric_mismatch": False, "slice_count": 100,
    "slice_thickness": 2.0,
}


def flags_for(**overrides):
    return ENGINE._evaluate_rules(dict(BASE_METRICS, **overrides))


def flag(flags, name, contains=""):
    matches = [f for f in flags if f.name == name and contains.lower() in f.message.lower()]
    assert len(matches) == 1, [(f.name, f.status, f.message) for f in flags if f.name == name]
    return matches[0]


# --- status model -------------------------------------------------------------

def test_info_never_escalates():
    assert series_verdict(["INFO", "INFO", "ACCEPT", "SKIPPED"]) == QAStatus.ACCEPT
    assert series_verdict(["INFO", "CONDITIONAL"]) == QAStatus.CONDITIONAL
    assert not is_actionable("INFO") and is_actionable("CONDITIONAL") and is_actionable("FAIL_CRITICAL")
    assert severity("CONDITIONAL") < severity("INFO") < severity("ACCEPT")


def test_all_info_findings_keep_series_accepted():
    flags = flags_for(metal_internal_cc=1.0, metal_surface_cc=5.0, metal_external_cc=2.0,
                      roll_deg=2.0, empty_slices=[1, 2])
    assert {f.status for f in flags} <= {QAStatus.ACCEPT, QAStatus.INFO, QAStatus.SKIPPED}
    assert any(f.status == QAStatus.INFO for f in flags)
    assert series_verdict(f.status for f in flags) == QAStatus.ACCEPT


def test_every_check_reports_a_flag_every_time():
    counts = {}
    for f in flags_for():
        counts[f.name] = counts.get(f.name, 0) + 1
    assert counts == {
        "GeometryGuardian": 5,   # FOV truncation, spacing, order, duplicates, gantry tilt
        "NoiseWhisperer": 2,     # background noise, air calibration
        "FluidPhysicist": 2,     # fluid HU, rescale slope
        "CavityScout": 1,        # gas (SKIPPED: not a pelvis/abdomen protocol)
        "ImplantAuditor": 3,     # internal, surface, external
        "AlignmentAuditor": 1,
        "Integrity": 3,          # paediatric markers, slice count, slice thickness
    }


@pytest.mark.parametrize("name,contains,value_text,limit_text", [
    ("GeometryGuardian", "spacing", "0.00 mm", "limit 1 mm"),
    ("GeometryGuardian", "gantry", "0°", "limit 1°"),
    ("NoiseWhisperer", "noise", "5.0 HU", "limit 15 HU"),
    ("NoiseWhisperer", "air hu", "-1000.0 HU", "-1100 to -900"),
    ("FluidPhysicist", "fluid density", "20.0 HU", "optimal 0 to 40"),
    ("Integrity", "thickness", "2 mm", "absolute limit 5 mm"),
    ("AlignmentAuditor", "roll", "+0.0°", "alert >3°"),
])
def test_flag_messages_carry_value_and_threshold(name, contains, value_text, limit_text):
    message = flag(flags_for(), name, contains).message
    assert value_text in message and limit_text in message


def test_problem_log_only_keeps_actionable_results():
    uid = "1.2.826.0.1.777"
    info_only = QAResult(series_uid=uid, status="ACCEPT", metrics={}, flags=[
        QAFlag(name="ImplantAuditor", status="INFO", message="small"),
        QAFlag(name="GeometryGuardian", status="ACCEPT", message="ok")])
    assert log_qa_result(info_only) is None
    assert not any(r["series_uid"] == uid for r in get_all_logs())

    problem = info_only.model_copy(update={"status": "CONDITIONAL", "flags": info_only.flags + [
        QAFlag(name="AlignmentAuditor", status="CONDITIONAL", message="ROLL_ALERT")]})
    record = log_qa_result(problem)
    assert [f["status"] for f in record["flags"]] == ["INFO", "ACCEPT", "CONDITIONAL"]  # full list kept
    assert record["issues"] == ["AlignmentAuditor: ROLL_ALERT"]
    assert sum(r["series_uid"] == uid for r in get_all_logs()) == 1

    # Re-analysed without actionable findings: the old record disappears
    log_qa_result(info_only)
    assert not any(r["series_uid"] == uid for r in get_all_logs())


# --- per-protocol overrides ----------------------------------------------------------

def test_protocol_overrides_match_substring_in_order(tmp_path):
    path = tmp_path / "ctqa.yaml"
    path.write_text(
        "protocol_overrides:\n"
        "  - match: thorax\n    thresholds: {slice_thickness: {nominal_mm: 3.0}}\n"
        "  - match: THORAX LOW\n    thresholds: {slice_thickness: {nominal_mm: 5.0}}\n")
    cfg = load_qa_config(str(path))
    assert cfg.thresholds_for("RTP Thorax Mellkas").slice_thickness.nominal_mm == 3.0
    assert cfg.thresholds_for("RTP THORAX low dose").slice_thickness.nominal_mm == 5.0
    assert cfg.thresholds_for("RTP Pelvis").slice_thickness.nominal_mm is None


def test_invalid_protocol_override_fails_at_load(tmp_path):
    path = tmp_path / "ctqa.yaml"
    path.write_text("protocol_overrides:\n  - match: HEAD\n    thresholds: {slice_thickness: {nominal: 2}}\n")
    with pytest.raises(ValueError, match="HEAD"):
        load_qa_config(str(path))


# --- slice thickness -----------------------------------------------------------------

@pytest.fixture
def thorax_nominal_engine(tmp_path):
    path = tmp_path / "ctqa.yaml"
    path.write_text("protocol_overrides:\n  - match: THORAX\n    thresholds: {slice_thickness: {nominal_mm: 3.0, tolerance_mm: 0.5}}\n")
    return QAEngine(str(path))


@pytest.mark.parametrize("protocol,thickness,expected", [
    ("RTP Thorax", 3.0, "ACCEPT"),
    ("RTP Thorax", 3.5, "ACCEPT"),       # within tolerance
    ("RTP Thorax", 2.0, "CONDITIONAL"),  # deviates from nominal
    ("RTP Thorax", 4.0, "CONDITIONAL"),
    ("RTP Thorax", 6.0, "REJECT"),       # absolute limit
    ("RTP Pelvis", 4.0, "ACCEPT"),       # no nominal configured: only the absolute limit applies
    ("RTP Pelvis", 5.5, "REJECT"),
])
def test_slice_thickness_flags_only_deviation_from_nominal(thorax_nominal_engine, protocol, thickness, expected):
    flags = thorax_nominal_engine._evaluate_rules(dict(BASE_METRICS, protocol=protocol, slice_thickness=thickness))
    f = flag(flags, "Integrity", "thickness")
    assert f.status == expected
    assert f"{thickness:g} mm" in f.message


# --- metal ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls,volume,expected", [
    ("internal", 0.0, "ACCEPT"), ("internal", 1.9, "INFO"), ("internal", 2.0, "CONDITIONAL"),
    ("surface", 9.9, "INFO"), ("surface", 10.0, "CONDITIONAL"),
    ("external", 4.9, "INFO"), ("external", 5.0, "CONDITIONAL"),
])
def test_metal_class_tiers(cls, volume, expected):
    f = flag(flags_for(**{f"metal_{cls}_cc": volume}), "ImplantAuditor", f"{cls.upper()}_METAL")
    assert f.status == expected


def test_pelvis_internal_metal_always_conditional_from_5cc(tmp_path):
    path = tmp_path / "ctqa.yaml"
    path.write_text("protocol_overrides:\n  - match: RTP\n    thresholds: {implants: {internal_info_max_cc: 20}}\n")
    engine = QAEngine(str(path))
    run = lambda pelvis, cc: flag(engine._evaluate_rules(dict(
        BASE_METRICS, protocol="RTP X", is_pelvis_scan=pelvis, metal_internal_cc=cc)), "ImplantAuditor", "INTERNAL")
    assert run(False, 6.0).status == "INFO"         # override raised the internal limit to 20 cc
    assert run(True, 6.0).status == "CONDITIONAL"   # pelvis: >= 5 cc regardless
    assert "pelvis limit 5 cc" in run(True, 6.0).message
    assert run(True, 4.0).status == "INFO"


# --- gas -------------------------------------------------------------------------------

@pytest.mark.parametrize("protocol,cc,expected,label", [
    ("RTP Pelvis", 0.0, "ACCEPT", "No internal gas"),
    ("RTP Pelvis", 12.0, "INFO", "within physiological limits"),
    ("RTP Pelvis", 30.0, "CONDITIONAL", "Moderate gas"),
    ("RTP Pelvis", 74.0, "CONDITIONAL", "Moderate gas"),
    ("RTP Pelvis", 120.0, "CONDITIONAL", "Large gas"),
    ("RTP Pelvis", 151.0, "REJECT", "Excessive gas"),
    ("RTP Abdomen", 400.0, "CONDITIONAL", "Large gas"),  # shipped ABD override: never reject on volume
])
def test_gas_tiers(protocol, cc, expected, label):
    flags = flags_for(protocol=protocol, is_pelvis_or_abdomen_scan=True, gas_volume_cc=cc,
                      gas_slices=[3, 4] if cc else [], gas_body_fraction=0.01, body_mask_sane=True)
    f = flag(flags, "CavityScout", "gas volume" if cc else "no internal gas")
    assert f.status == expected and label in f.message
    assert flag(flags, "CavityScout", "sanity").status == "ACCEPT"


def test_body_mask_sanity_replaces_segmentation_leak():
    flags = flags_for(protocol="RTP Pelvis", is_pelvis_or_abdomen_scan=True, gas_volume_cc=400.0,
                      gas_body_fraction=0.4, body_mask_sane=False)
    cavity = [f for f in flags if f.name == "CavityScout"]
    assert [f.status for f in cavity] == ["CONDITIONAL", "INFO"]
    assert "BODY_MASK_SANITY" in cavity[0].message and "40.0%" in cavity[0].message
    assert "unreliable" in cavity[1].message
    assert not any("SEGMENTATION_LEAK" in f.message for f in flags)


# --- roll ------------------------------------------------------------------------------

N = 512


def rotate_patient(img, roll_deg):
    """Apply a patient roll in the reporting convention (positive = clockwise on screen)."""
    return ndi.rotate(img, -roll_deg, reshape=False, order=1, cval=-1000) if roll_deg else img


def body_phantom(shift=0, roll=0.0, a=170, b=110):
    y, x = np.ogrid[:N, :N]
    cy, cx = (N - 1) / 2, (N - 1) / 2 + shift
    img = np.full((N, N), -1000.0)
    img[((x - cx) / a) ** 2 + ((y - cy) / b) ** 2 <= 1] = 40
    img[((x - cx) / 25) ** 2 + ((y - (cy + b - 35)) / 20) ** 2 <= 1] = 700  # posterior spine
    return rotate_patient(img, roll)


def head_phantom(roll=0.0, shift=0):
    y, x = np.ogrid[:N, :N]
    cy, cx = (N - 1) / 2, (N - 1) / 2 + shift
    img = np.full((N, N), -1000.0)
    img[((x - cx) / 80) ** 2 + ((y - cy) / 95) ** 2 <= 1] = 1000           # skull
    img[((x - cx) / 72) ** 2 + ((y - cy) / 87) ** 2 <= 1] = 35             # brain
    img[((x - cx) / 12) ** 2 + ((y - (cy - 100)) / 14) ** 2 <= 1] = 20     # nose (anterior)
    img[((x - cx) / 30) ** 2 + ((y - (cy + 40)) / 20) ** 2 <= 1] = 60      # posterior fossa
    return rotate_patient(img, roll)


ALIGN = AlignmentThresholds()


@pytest.mark.parametrize("shift", [0, 30, 60])
@pytest.mark.parametrize("roll", [0.0, 3.0, -6.0])
def test_roll_recovered_on_body_phantom(shift, roll):
    est = estimate_roll(body_phantom(shift, roll), ALIGN)
    assert est["reliable"]
    assert abs(est["roll_deg"] - roll) <= 0.5


@pytest.mark.parametrize("shift", [0, 40])
@pytest.mark.parametrize("roll", [0.0, 4.0, -3.0])
def test_roll_recovered_on_near_circular_head(shift, roll):
    est = estimate_roll(head_phantom(roll, shift), ALIGN)
    assert est["reliable"]
    assert abs(est["roll_deg"] - roll) <= 1.0


def test_centred_unrotated_ellipse_reports_zero_not_search_limit():
    # The previous Radon estimator returned the +10° sweep limit here
    y, x = np.ogrid[:N, :N]
    img = np.where(((x - 255.5) / 170) ** 2 + ((y - 255.5) / 110) ** 2 <= 1, 40.0, -1000.0)
    assert estimate_roll(img, ALIGN)["roll_deg"] == 0.0


def test_asymmetric_slice_is_unreliable_not_an_alert():
    y, x = np.ogrid[:N, :N]
    img = np.full((N, N), -1000.0)
    img[((x - 200) / 120) ** 2 + ((y - 256) / 80) ** 2 <= 1] = 40
    img[((x - 300) / 60) ** 2 + ((y - 200) / 60) ** 2 <= 1] = 40
    est = estimate_roll(img, ALIGN)
    assert not est["reliable"]
    f = flag(flags_for(roll_deg=est["roll_deg"], roll_correlation=est["correlation"], roll_reliable=False,
                       roll_unreliable_reason=est["reason"]), "AlignmentAuditor")
    assert f.status == "INFO" and "unreliable" in f.message


@pytest.mark.parametrize("roll,expected", [(1.0, "ACCEPT"), (-2.0, "INFO"), (3.0, "INFO"), (3.5, "CONDITIONAL"), (-8.0, "CONDITIONAL")])
def test_roll_thresholds(roll, expected):
    assert flag(flags_for(roll_deg=roll), "AlignmentAuditor").status == expected


# --- truncation (integration through the engine) ----------------------------------

R, SLICES, SPACING = 160, 20, 2.0


def _write_series(directory, volume, protocol="RTP Thorax"):
    paths = []
    for i, sl in enumerate(volume):
        ds = Dataset()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
        ds.SOPInstanceUID = f"1.2.3.4.{i + 1}"
        ds.SeriesInstanceUID = "1.2.3.4"
        ds.Modality = "CT"
        ds.PatientName = "Phantom"
        ds.ProtocolName = protocol
        ds.Rows = ds.Columns = R
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 12, 11
        ds.PixelRepresentation, ds.SamplesPerPixel = 0, 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelSpacing = [SPACING, SPACING]
        ds.SliceThickness = SPACING
        ds.ImagePositionPatient = [0.0, 0.0, float(i * SPACING)]
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


def _torso(ax=55, ay=40):
    y, x = np.ogrid[:R, :R]
    return ((x - 79.5) / ax) ** 2 + ((y - 79.5) / ay) ** 2 <= 1


def _analyze(tmp_path, slice_masks):
    vol = np.full((SLICES, R, R), -1000.0)
    for i in range(SLICES):
        vol[i][slice_masks(i)] = 30.0
    return ENGINE.analyze_series(_write_series(str(tmp_path), vol))


def _fov_flag(result):
    return [f for f in result.flags if f.name == "GeometryGuardian"][0]


def test_clear_torso_has_no_truncation(tmp_path):
    result = _analyze(tmp_path, lambda i: _torso())
    assert _fov_flag(result).status == "ACCEPT"
    assert not result.metrics["truncation_detected"]


def test_anterior_posterior_contact_is_critical_even_on_one_slice(tmp_path):
    result = _analyze(tmp_path, lambda i: _torso(ay=90) if i == 10 else _torso())
    assert result.metrics["anterior_posterior_truncated_slices"] == [11]
    assert _fov_flag(result).status == "REJECT"
    assert "TRUNCATION_ERROR" in _fov_flag(result).message and "Slice 11" in _fov_flag(result).message


@pytest.mark.parametrize("n_slices,expected", [(3, "CONDITIONAL"), (6, "CONDITIONAL"), (7, "REJECT"), (12, "REJECT")])
def test_lateral_torso_truncation_graded_by_z_extent(tmp_path, n_slices, expected):
    # 2 mm slices: 6 slices = 12 mm (limit, CONDITIONAL), 7 slices = 14 mm (REJECT)
    result = _analyze(tmp_path, lambda i: _torso(ax=90) if 5 <= i < 5 + n_slices else _torso())
    assert result.metrics["lateral_torso_truncated_slices"] == list(range(6, 6 + n_slices))
    assert result.metrics["lateral_truncation_z_extent_mm"] == pytest.approx(n_slices * SPACING)
    assert result.metrics["max_contact_length_mm"] > 0
    f = _fov_flag(result)
    assert f.status == expected
    assert f"{n_slices * SPACING:.1f} mm z-extent" in f.message


def test_arm_at_fov_edge_with_clear_torso_is_info(tmp_path):
    y, x = np.ogrid[:R, :R]
    arm = (x < 22) & (y >= 70) & (y < 90)                  # 44 x 40 mm arm touching the left edge
    bridge = (x >= 22) & (x < 30) & (y >= 78) & (y < 82)   # 8 mm contact with the torso
    result = _analyze(tmp_path, lambda i: _torso() | arm | bridge if 8 <= i < 16 else _torso())
    f = _fov_flag(result)
    assert f.status == "INFO", f.message
    assert not result.metrics["truncation_error"]
    assert result.metrics["truncated_slices"] == []
    assert result.status != "REJECT"


def test_empty_slices_still_bypass_truncation(tmp_path):
    result = _analyze(tmp_path, lambda i: np.zeros((R, R), bool) if i in (0, 19) else _torso())
    assert result.metrics["empty_slices"] == [1, 20]
    assert _fov_flag(result).status == "ACCEPT"


# --- 4DCT metal policy -------------------------------------------------------------

def _metal_result(uid, internal_cc):
    metrics = dict(BASE_METRICS, metal_internal_cc=internal_cc, metal_internal_slices=[4])
    flags = ENGINE._evaluate_rules(metrics)
    return QAResult(series_uid=uid, status=series_verdict(f.status for f in flags), metrics=metrics, flags=flags)


def test_metal_is_evaluated_once_per_4dct_group():
    ref, phase = _metal_result("1.2.3.10", 3.0), _metal_result("1.2.3.11", 3.0)
    assert ref.status == phase.status == "CONDITIONAL"

    deferred = ENGINE.with_metal_policy(phase, "1.2.3.10")
    metal = [f for f in deferred.flags if f.name == "ImplantAuditor"]
    assert [f.status for f in metal] == ["INFO"] and "1.2.3.10" in metal[0].message
    assert deferred.status == "ACCEPT"
    assert ENGINE.with_metal_policy(ref, "1.2.3.10") == ref          # reference keeps its flags

    # The phase becoming the reference restores its per-class flags
    restored = ENGINE.with_metal_policy(deferred, None)
    assert restored.flags == phase.flags and restored.status == "CONDITIONAL"


def test_group_policy_updates_cached_phase_results(tmp_path):
    from backend import state
    from backend.fourdct import FourDCTGroup, TemporalPhase

    ref, phase = _metal_result("1.2.3.20", 3.0), _metal_result("1.2.3.21", 3.0)
    group = FourDCTGroup(group_id="g", study_instance_uid="1", frame_of_reference_uid="2", patient_name="P",
                         patient_id="1", study_date="", series_description="4D", phases=[
        TemporalPhase(series_uid=ref.series_uid, temporal_position=1, phase_label="P1", series_dir=""),
        TemporalPhase(series_uid=phase.series_uid, temporal_position=2, phase_label="P2", series_dir="")])
    group.reference_phase_uid = ref.series_uid

    with patch.object(settings, "STORAGE_DIR", str(tmp_path)), patch.object(settings, "REPORTS_DIR", str(tmp_path)), \
         patch.dict(state.results_cache, {ref.series_uid: ref, phase.series_uid: phase}, clear=True):
        os.makedirs(tmp_path / phase.series_uid)
        assert state.apply_group_metal_policy([group]) == [phase.series_uid]
        assert state.results_cache[phase.series_uid].status == "ACCEPT"
        assert state.results_cache[ref.series_uid].status == "CONDITIONAL"
        assert (tmp_path / phase.series_uid / "qa_result.json").exists()
        assert state.apply_group_metal_policy([group]) == []          # idempotent
