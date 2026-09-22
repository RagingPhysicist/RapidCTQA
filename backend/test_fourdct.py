"""
backend/test_fourdct.py
========================
Unit tests for the 4DCT temporal series grouping module.

Tests run without any real DICOM files — all DICOM datasets are synthesised
with pydicom in-memory objects.

Coverage:
 - Normal CT (no grouping): 3 unrelated series → all plain
 - 4DCT 10 phases: 10 series sharing StudyInstanceUID + FrameOfReferenceUID
   + TemporalPositionIdentifier → one group
 - 4DCT variable phases: 5, 8, 12 phases
 - False-positive guard: identical SeriesDescriptions but different
   StudyInstanceUID → no grouping
 - Mixed study: 1 plain series + 10 4DCT phases → 1 plain + 1 group
 - Missing temporal metadata: same geometry + FOR + study but no
   TemporalPositionIdentifier → no grouping (safe default)
 - Missing StudyInstanceUID: no grouping
 - Too few slices per candidate (< MIN_SLICES_PER_PHASE): excluded
 - Reference phase selection: median temporal position
"""

import os
import glob
import pytest
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from typing import Dict, List, Optional

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import UID

# Ensure the project root is on sys.path
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.fourdct import (
    detect_fourdct_groups,
    FourDCTGroup,
    TemporalPhase,
    _build_group,
    _extract_candidate,
    _select_reference_phase,
    MIN_SLICES_PER_PHASE,
    MIN_PHASES,
)


# ---------------------------------------------------------------------------
# Helpers to build synthetic DICOM files on disk
# ---------------------------------------------------------------------------

CT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.2"
TRANSFER_SYNTAX = "1.2.840.10008.1.2.1"


def _make_ct_dataset(
    series_uid: str,
    sop_uid: str,
    study_uid: str = "1.2.3.4.5",
    frame_of_reference_uid: str = "1.2.3.4.6",
    temporal_position: Optional[int] = None,
    n_temporal_positions: Optional[int] = None,
    series_description: str = "4DCT",
    rows: int = 64,
    cols: int = 64,
    slice_thickness: float = 3.0,
    image_orientation: Optional[List[float]] = None,
    z_position: float = 0.0,
) -> Dataset:
    ds = Dataset()
    ds.SOPClassUID = CT_SOP_CLASS
    ds.SOPInstanceUID = sop_uid
    ds.Modality = "CT"
    ds.SeriesInstanceUID = series_uid
    ds.StudyInstanceUID = study_uid
    ds.FrameOfReferenceUID = frame_of_reference_uid
    ds.SeriesDescription = series_description
    ds.StudyDate = "20240101"
    ds.PatientName = "Test^Patient"
    ds.PatientID = "P001"
    ds.Rows = rows
    ds.Columns = cols
    ds.SliceThickness = slice_thickness
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.PixelSpacing = [1.0, 1.0]
    ds.ImagePositionPatient = [0.0, 0.0, z_position]
    ds.ImageOrientationPatient = image_orientation or [1, 0, 0, 0, 1, 0]
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = -1024.0
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(rows * cols * 2)

    if temporal_position is not None:
        ds.TemporalPositionIdentifier = temporal_position
    if n_temporal_positions is not None:
        ds.NumberOfTemporalPositions = n_temporal_positions

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CT_SOP_CLASS
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = TRANSFER_SYNTAX
    ds.file_meta = file_meta
    ds.is_implicit_VR = False
    ds.is_little_endian = True
    return ds


def _write_series_dir(
    base_dir: str,
    series_uid: str,
    n_slices: int,
    **kwargs,
) -> str:
    """Write n_slices synthetic DICOM files into base_dir/series_uid/ and return the path."""
    series_dir = os.path.join(base_dir, series_uid)
    os.makedirs(series_dir, exist_ok=True)
    for i in range(n_slices):
        sop_uid = f"{series_uid}.{i + 1}"
        ds = _make_ct_dataset(
            series_uid=series_uid,
            sop_uid=sop_uid,
            z_position=float(i * 3),
            **kwargs,
        )
        path = os.path.join(series_dir, f"{i:04d}.dcm")
        ds.save_as(path, enforce_file_format=True)
    return series_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_storage(tmp_path):
    """Return a temporary directory acting as STORAGE_DIR."""
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormalCT:
    """Three unrelated CT series → all remain plain, no groups."""

    def test_three_independent_series(self, tmp_storage):
        dirs = {}
        for i in range(3):
            uid = f"1.2.3.series.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=f"1.2.study.{i}",          # different study UIDs!
                frame_of_reference_uid=f"1.2.for.{i}",
                series_description="CT THORAX",
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert groups == [], "No groups expected for independent series"
        assert len(plain_uids) == 3

    def test_same_description_different_study(self, tmp_storage):
        """Identical SeriesDescription but different StudyInstanceUID → no grouping."""
        dirs = {}
        for i in range(5):
            uid = f"1.2.series.same_desc.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=f"1.2.study.{i}",          # each in a different study
                frame_of_reference_uid=f"1.2.for.{i}",
                series_description="4DCT LUNG",       # same description!
                temporal_position=i + 1,
                n_temporal_positions=5,
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert groups == [], "Same description across different studies must not trigger grouping"
        assert len(plain_uids) == 5

    def test_too_few_slices_excluded(self, tmp_storage):
        """Series with fewer than MIN_SLICES_PER_PHASE slices are never grouped."""
        dirs = {}
        for i in range(10):
            uid = f"1.2.series.tiny.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=5,         # below MIN_SLICES_PER_PHASE=10
                study_uid="1.2.study.tiny",
                frame_of_reference_uid="1.2.for.tiny",
                temporal_position=i + 1,
                n_temporal_positions=10,
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert groups == [], "Tiny series must not be grouped"
        # They are returned as plain series (excluded from grouping analysis)
        assert len(plain_uids) == 10


class TestFourDCTGrouping:
    """4DCT acquisition → single logical group."""

    @pytest.mark.parametrize("n_phases", [2, 5, 8, 10, 12])
    def test_variable_phase_count(self, tmp_storage, n_phases):
        """Variable number of phases (2-12) should all produce exactly one group."""
        dirs = {}
        study_uid = "1.2.study.4dct"
        for_uid = "1.2.for.4dct"

        for i in range(n_phases):
            uid = f"1.2.series.4dct.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=i + 1,
                n_temporal_positions=n_phases,
                series_description="4DCT Free Breathing",
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert len(groups) == 1, f"Expected 1 group for {n_phases} phases"
        assert groups[0].phase_count == n_phases
        assert plain_uids == []

    def test_10_phase_4dct(self, tmp_storage):
        """Full 10-phase 4DCT: specific assertions on group properties."""
        dirs = {}
        study_uid = "1.2.study.10phase"
        for_uid = "1.2.for.10phase"

        for i in range(10):
            uid = f"1.2.4dct.phase.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=120,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=i + 1,
                n_temporal_positions=10,
                series_description="4DCT",
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert len(groups) == 1
        g = groups[0]
        assert g.phase_count == 10
        assert g.study_instance_uid == study_uid
        assert g.frame_of_reference_uid == for_uid
        assert g.reference_phase_uid != ""
        assert plain_uids == []
        # All 10 phase UIDs are accounted for in the group
        group_uids = {p.series_uid for p in g.phases}
        assert group_uids == set(dirs.keys())

    def test_phases_ordered_by_temporal_position(self, tmp_storage):
        """Phases should be ordered by temporal_position even if dirs are shuffled."""
        dirs = {}
        study_uid = "1.2.study.order"
        for_uid = "1.2.for.order"

        # Insert in reverse order
        for i in reversed(range(5)):
            uid = f"1.2.series.order.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=i + 1,
                n_temporal_positions=5,
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, _ = detect_fourdct_groups(dirs)
        assert len(groups) == 1
        positions = [p.temporal_position for p in groups[0].phases]
        assert positions == sorted(positions), "Phases must be sorted by temporal position"

    def test_group_id_format(self, tmp_storage):
        """group_id must be StudyInstanceUID::FrameOfReferenceUID."""
        dirs = {}
        study_uid = "1.2.study.gid"
        for_uid = "1.2.for.gid"
        for i in range(3):
            uid = f"1.2.series.gid.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=i + 1,
                n_temporal_positions=3,
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, _ = detect_fourdct_groups(dirs)
        assert groups[0].group_id == f"{study_uid}::{for_uid}"


class TestMixedStudy:
    """Study contains one plain series and one 4DCT acquisition."""

    def test_plain_plus_4dct(self, tmp_storage):
        dirs = {}

        # 1 plain CT series
        plain_uid = "1.2.series.plain"
        _write_series_dir(
            tmp_storage, plain_uid, n_slices=100,
            study_uid="1.2.study.plain",
            frame_of_reference_uid="1.2.for.plain",
            series_description="CT PELVIS",
        )
        dirs[plain_uid] = os.path.join(tmp_storage, plain_uid)

        # 10-phase 4DCT in a different study
        study_uid = "1.2.study.4dct"
        for_uid = "1.2.for.4dct"
        for i in range(10):
            uid = f"1.2.series.4dct.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=i + 1,
                n_temporal_positions=10,
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert len(groups) == 1
        assert groups[0].phase_count == 10
        assert plain_uid in plain_uids
        assert len(plain_uids) == 1

    def test_two_independent_4dct_acquisitions(self, tmp_storage):
        """Two 4DCT acquisitions within the same STORAGE_DIR → two groups."""
        dirs = {}
        for acq in range(2):
            study_uid = f"1.2.study.4dct.{acq}"
            for_uid = f"1.2.for.4dct.{acq}"
            for i in range(5):
                uid = f"1.2.acq.{acq}.series.{i}"
                _write_series_dir(
                    tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                    study_uid=study_uid,
                    frame_of_reference_uid=for_uid,
                    temporal_position=i + 1,
                    n_temporal_positions=5,
                )
                dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert len(groups) == 2
        assert plain_uids == []
        assert groups[0].group_id != groups[1].group_id


class TestMissingMetadata:
    """Safety: missing temporal metadata → no grouping."""

    def test_no_temporal_tag_no_grouping(self, tmp_storage):
        """Same StudyInstanceUID + FrameOfReferenceUID but NO temporal tags → not grouped."""
        dirs = {}
        study_uid = "1.2.study.notable"
        for_uid = "1.2.for.notable"

        for i in range(5):
            uid = f"1.2.series.notable.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                series_description="CT",
                # No temporal_position, no n_temporal_positions
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert groups == [], "No temporal signal → must not group"
        assert len(plain_uids) == 5

    def test_temporal_tag_not_varying(self, tmp_storage):
        """All series report the same TemporalPositionIdentifier → no group."""
        dirs = {}
        study_uid = "1.2.study.sametp"
        for_uid = "1.2.for.sametp"

        for i in range(5):
            uid = f"1.2.series.sametp.{i}"
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=1,          # same for all!
                # No NumberOfTemporalPositions
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)

        assert groups == [], "Non-varying TemporalPositionIdentifier → must not group"

    def test_missing_study_uid_no_grouping(self, tmp_storage):
        """Series without StudyInstanceUID → treated as plain series."""
        dirs = {}
        for i in range(5):
            uid = f"1.2.series.nostudyuid.{i}"
            series_dir = os.path.join(tmp_storage, uid)
            os.makedirs(series_dir, exist_ok=True)
            for j in range(MIN_SLICES_PER_PHASE):
                sop = f"{uid}.{j}"
                ds = _make_ct_dataset(
                    series_uid=uid, sop_uid=sop,
                    study_uid="",               # empty!
                    frame_of_reference_uid="1.2.for.X",
                    temporal_position=i + 1,
                    n_temporal_positions=5,
                )
                del ds.StudyInstanceUID      # remove entirely
                pydicom.dcmwrite(os.path.join(series_dir, f"{j:04d}.dcm"), ds)
            dirs[uid] = series_dir

        groups, plain_uids = detect_fourdct_groups(dirs)
        assert groups == [], "Missing StudyInstanceUID → no grouping"

    def test_geometry_mismatch_no_grouping(self, tmp_storage):
        """Same studyUID + FOR + temporal tags, but different geometry → no group."""
        dirs = {}
        study_uid = "1.2.study.geomix"
        for_uid = "1.2.for.geomix"

        for i in range(5):
            uid = f"1.2.series.geomix.{i}"
            # Each phase has a different Rows value → geometry mismatch
            _write_series_dir(
                tmp_storage, uid, n_slices=MIN_SLICES_PER_PHASE,
                study_uid=study_uid,
                frame_of_reference_uid=for_uid,
                temporal_position=i + 1,
                n_temporal_positions=5,
                rows=64 + i * 8,              # different each time!
            )
            dirs[uid] = os.path.join(tmp_storage, uid)

        groups, plain_uids = detect_fourdct_groups(dirs)
        assert groups == [], "Geometry mismatch → must not group"

    def test_empty_storage_dir(self):
        """Empty series_dirs dict → no groups, no plain series."""
        groups, plain_uids = detect_fourdct_groups({})
        assert groups == []
        assert plain_uids == []


class TestReferencePhaseSelection:
    """The reference phase must be the median temporal position."""

    def test_median_phase_selected_odd(self):
        phases = [
            TemporalPhase("uid1", 1, "Phase 1", "/dir/1"),
            TemporalPhase("uid2", 2, "Phase 2", "/dir/2"),
            TemporalPhase("uid3", 3, "Phase 3", "/dir/3"),
            TemporalPhase("uid4", 4, "Phase 4", "/dir/4"),
            TemporalPhase("uid5", 5, "Phase 5", "/dir/5"),
        ]
        ref = _select_reference_phase(phases)
        assert ref == "uid3", f"Expected uid3 (pos=3), got {ref}"

    def test_median_phase_selected_even(self):
        phases = [
            TemporalPhase("uid1", 1, "Phase 1", "/dir/1"),
            TemporalPhase("uid2", 2, "Phase 2", "/dir/2"),
            TemporalPhase("uid3", 3, "Phase 3", "/dir/3"),
            TemporalPhase("uid4", 4, "Phase 4", "/dir/4"),
        ]
        ref = _select_reference_phase(phases)
        # median index for 4 items = index 2 (0-based) → uid3 or uid4 both valid
        assert ref in {"uid3", "uid4"}

    def test_single_phase(self):
        phases = [TemporalPhase("only", 1, "Phase 1", "/dir/1")]
        assert _select_reference_phase(phases) == "only"

    def test_empty_phases(self):
        assert _select_reference_phase([]) == ""

    def test_10_phase_median(self):
        phases = [
            TemporalPhase(f"uid{i}", i, f"Phase {i}", f"/dir/{i}")
            for i in range(1, 11)
        ]
        ref = _select_reference_phase(phases)
        # sorted positions: [1..10], median index=5 (0-based), value=6 → uid6
        assert ref == "uid6"
