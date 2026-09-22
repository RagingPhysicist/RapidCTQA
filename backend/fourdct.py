"""
backend/fourdct.py
==================
Temporal series grouping — detects when multiple DICOM CT series stored on disk
belong to the same 4D CT acquisition (one series per temporal phase/time-point)
and returns logical FourDCTGroup objects for use by the API layer.

Design goals:
 - Zero side-effects: no file writes, no network calls.
 - The grouping key is: StudyInstanceUID + FrameOfReferenceUID.
 - A group must have ≥ 2 temporal series; a single series is never grouped.
 - Detection priority:
     1. TemporalPositionIdentifier present and varying across candidate series.
     2. NumberOfTemporalPositions > 1 present on any slice (definitive).
     3. Identical geometric fingerprint (Rows, Columns, SliceThickness,
        ImageOrientationPatient rounded to 2 dp) + ≥ MIN_SLICES_PER_PHASE slices.
 - False-positive guard: identical SeriesDescription alone is NOT sufficient.
   The series must share StudyInstanceUID + FrameOfReferenceUID.
"""

from __future__ import annotations

import os
import glob
import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pydicom

logger = logging.getLogger(__name__)

# A candidate series must have at least this many slices to be considered
# a real acquisition phase (excludes localisers / dose-reports that happen
# to share metadata).
MIN_SLICES_PER_PHASE = 10

# Minimum number of distinct series to form a group.
MIN_PHASES = 2

# Patterns in SeriesDescription that indicate a 4D CT or respiratory-correlated acquisition
FOUR_D_PATTERNS = [
    r'\b4d\b', r'\b4dct\b', r'respiratory', r'tps_sort', r'\bmaxip\b', r'\bminip\b',
    r'average\s+ct', r'avg\s+ct', r'\bphase\b', r'\d+%\b', r'\b(in|ex)\b'
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TemporalPhase:
    """One temporal phase/time-point within a 4DCT acquisition."""
    series_uid: str
    temporal_position: int          # 1-based; inferred if tag absent
    phase_label: str                # "Phase 1", "Phase 2", …
    series_dir: str                 # absolute path to series directory
    instance_count: int = 0
    status: str = "PENDING"         # filled in by the API layer from results_cache

    def to_dict(self) -> dict:
        return {
            "series_uid": self.series_uid,
            "temporal_position": self.temporal_position,
            "phase_label": self.phase_label,
            "instance_count": self.instance_count,
            "status": self.status,
        }


@dataclass
class FourDCTGroup:
    """
    A logical 4DCT examination comprising multiple temporal phases.

    The underlying DICOM series are never modified; this object is a
    read-only view constructed purely from DICOM header metadata.
    """
    group_id: str                       # StudyInstanceUID::FrameOfReferenceUID
    study_instance_uid: str
    frame_of_reference_uid: str
    patient_name: str
    patient_id: str
    study_date: str
    series_description: str             # representative description
    phases: List[TemporalPhase] = field(default_factory=list)
    reference_phase_uid: str = ""       # UID of the selected reference phase
    status: str = "PENDING"             # aggregated across phases
    modality: str = "CT"

    @property
    def phase_count(self) -> int:
        return len(self.phases)

    @property
    def instance_count(self) -> int:
        return sum(p.instance_count for p in self.phases)

    def to_dict(self) -> dict:
        return {
            "type": "4dct_group",
            "group_id": self.group_id,
            "study_instance_uid": self.study_instance_uid,
            "frame_of_reference_uid": self.frame_of_reference_uid,
            "patient_name": self.patient_name,
            "patient_id": self.patient_id,
            "study_date": self.study_date,
            "series_description": self.series_description,
            "phase_count": self.phase_count,
            "reference_phase_uid": self.reference_phase_uid,
            "status": self.status,
            "modality": self.modality,
            "instance_count": self.instance_count,
            "phases": [p.to_dict() for p in self.phases],
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_header(dcm_path: str) -> Optional[pydicom.Dataset]:
    """Read only the DICOM header (no pixel data). Returns None on failure."""
    try:
        return pydicom.dcmread(dcm_path, stop_before_pixels=True, force=True)
    except Exception as exc:
        logger.debug("Cannot read DICOM header from %s: %s", dcm_path, exc)
        return None


def _orientation_key(ds: pydicom.Dataset) -> Optional[Tuple]:
    """
    Return a rounded tuple representing ImageOrientationPatient, or None.
    Two series with the same orientation key share the same imaging plane.
    """
    iop = getattr(ds, "ImageOrientationPatient", None)
    if iop is None or len(iop) < 6:
        return None
    try:
        return tuple(round(float(v), 1) for v in iop)
    except (TypeError, ValueError):
        return None


def _geometry_key(ds: pydicom.Dataset) -> Optional[Tuple]:
    """
    Fingerprint for slice geometry: (Rows, Cols, SliceThickness_rounded, orientation).
    Two series with the same geometry key are spatially compatible.
    """
    rows = getattr(ds, "Rows", None)
    cols = getattr(ds, "Columns", None)
    thickness = getattr(ds, "SliceThickness", None)
    orientation = _orientation_key(ds)

    if None in (rows, cols, thickness, orientation):
        return None
    try:
        return (int(rows), int(cols), round(float(thickness), 1), orientation)
    except (TypeError, ValueError):
        return None


def _grouping_key(ds: pydicom.Dataset) -> Optional[str]:
    """
    Primary grouping key: StudyInstanceUID + FrameOfReferenceUID.
    Returns None if either attribute is absent or empty.
    """
    study_uid = str(getattr(ds, "StudyInstanceUID", "")).strip()
    for_uid = str(getattr(ds, "FrameOfReferenceUID", "")).strip()
    if not study_uid or not for_uid:
        return None
    return f"{study_uid}::{for_uid}"


def _is_ct_image(ds: pydicom.Dataset) -> bool:
    """Return True iff the dataset is a plain axial CT image (not RTSS, localiser, etc.)."""
    if getattr(ds, "SOPClassUID", "") != "1.2.840.10008.5.1.4.1.1.2":
        return False
    image_type = getattr(ds, "ImageType", [])
    if any("LOCALIZER" in str(t).upper() for t in image_type):
        return False
    return True


def _representative_header(series_dir: str) -> Optional[pydicom.Dataset]:
    """Return the DICOM header of the first CT image found in a series directory."""
    dcm_files = sorted(glob.glob(os.path.join(series_dir, "*.dcm")))
    for path in dcm_files:
        ds = _read_header(path)
        if ds is not None and _is_ct_image(ds):
            return ds
    return None


def _count_ct_files(series_dir: str) -> int:
    """Count .dcm files in directory (fast approximation — no header read)."""
    return len(glob.glob(os.path.join(series_dir, "*.dcm")))


def _temporal_position(ds: pydicom.Dataset) -> Optional[int]:
    """Extract TemporalPositionIdentifier as int, or None if absent."""
    val = getattr(ds, "TemporalPositionIdentifier", None)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _number_of_temporal_positions(ds: pydicom.Dataset) -> Optional[int]:
    """Extract NumberOfTemporalPositions as int, or None if absent."""
    val = getattr(ds, "NumberOfTemporalPositions", None)
    if val is None:
        return None
    try:
        n = int(val)
        return n if n > 1 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Candidate series extraction
# ---------------------------------------------------------------------------

@dataclass
class _SeriesCandidate:
    """Internal: metadata extracted from one series directory for grouping."""
    series_uid: str
    series_dir: str
    instance_count: int
    grouping_key: Optional[str]       # StudyInstanceUID::FrameOfReferenceUID
    study_instance_uid: str
    frame_of_reference_uid: str
    series_description: str
    series_number: int
    geometry_key: Optional[Tuple]
    temporal_position: Optional[int]   # TemporalPositionIdentifier
    number_of_temporal_positions: Optional[int]
    patient_name: str
    patient_id: str
    study_date: str


def _extract_candidate(series_uid: str, series_dir: str) -> Optional[_SeriesCandidate]:
    """
    Read one representative DICOM header from a series directory and
    extract all attributes needed for grouping. Returns None if no
    valid CT file is found or if essential metadata is missing.
    """
    instance_count = _count_ct_files(series_dir)
    if instance_count < MIN_SLICES_PER_PHASE:
        return None  # Too few slices — cannot be a 4DCT phase

    ds = _representative_header(series_dir)
    if ds is None:
        return None

    gkey = _grouping_key(ds)
    # We still create a candidate even without a grouping key; it will be
    # treated as a plain series since gkey is None.

    try:
        snum = int(getattr(ds, "SeriesNumber", 0))
    except (TypeError, ValueError):
        snum = 0

    return _SeriesCandidate(
        series_uid=series_uid,
        series_dir=series_dir,
        instance_count=instance_count,
        grouping_key=gkey,
        study_instance_uid=str(getattr(ds, "StudyInstanceUID", "")).strip(),
        frame_of_reference_uid=str(getattr(ds, "FrameOfReferenceUID", "")).strip(),
        series_description=str(getattr(ds, "SeriesDescription", "")).strip(),
        series_number=snum,
        geometry_key=_geometry_key(ds),
        temporal_position=_temporal_position(ds),
        number_of_temporal_positions=_number_of_temporal_positions(ds),
        patient_name=str(getattr(ds, "PatientName", "Unknown")).strip(),
        patient_id=str(getattr(ds, "PatientID", "Unknown")).strip(),
        study_date=str(getattr(ds, "StudyDate", "Unknown")).strip(),
    )


# ---------------------------------------------------------------------------
# Grouping logic
# ---------------------------------------------------------------------------

def _select_reference_phase(
    phases: List[TemporalPhase],
    candidates: Optional[List[_SeriesCandidate]] = None,
) -> str:
    """
    Choose the reference phase for segmentation.

    Strategy:
    1. If a candidate is explicitly labeled 'Average CT' or 'Avg CT', prioritize it.
    2. Otherwise, select the phase whose temporal_position is closest to
       the median temporal position.
    3. If all temporal positions are equal (i.e., inferred), return the
       middle phase by list index.
    """
    if not phases:
        return ""
    if candidates:
        for c in candidates:
            if re.search(r'average\s+ct|avg\s+ct', c.series_description, re.IGNORECASE):
                return c.series_uid
    positions = [p.temporal_position for p in phases]
    median_pos = sorted(positions)[len(positions) // 2]
    best = min(phases, key=lambda p: abs(p.temporal_position - median_pos))
    return best.series_uid


def _build_group(
    gkey: str,
    candidates: List[_SeriesCandidate],
) -> Optional[FourDCTGroup]:
    """
    Given a list of candidates sharing a grouping key, decide whether they
    form a valid 4DCT group and, if so, return the FourDCTGroup.

    A group is valid when ALL of the following hold:
    1. At least MIN_PHASES candidates.
    2. All candidates share the same geometry_key (same FOV, slice thickness,
       orientation) — ensures they are spatially co-registered.
    3. At least one of:
       a. TemporalPositionIdentifier varies across the candidates, OR
       b. NumberOfTemporalPositions > 1 is present on any candidate, OR
       c. Series descriptions match 4D/respiratory/phase patterns.
    """
    if len(candidates) < MIN_PHASES:
        return None

    # Guard 1: geometry consistency
    geometry_keys = {c.geometry_key for c in candidates if c.geometry_key is not None}
    if len(geometry_keys) != 1:
        # Mixed geometries — not the same acquisition
        logger.debug(
            "Grouping key %s: geometry mismatch (%d distinct keys) — not grouping.",
            gkey, len(geometry_keys),
        )
        return None

    # Guard 2: temporal signal
    temporal_positions = [c.temporal_position for c in candidates]
    has_temporal_tag = any(t is not None for t in temporal_positions)
    positions_vary = len(set(t for t in temporal_positions if t is not None)) > 1
    has_n_temporal = any(c.number_of_temporal_positions is not None for c in candidates)
    has_desc_signal = any(
        any(re.search(pat, c.series_description, re.IGNORECASE) for pat in FOUR_D_PATTERNS)
        for c in candidates
    )

    if has_temporal_tag:
        is_temporal = positions_vary or has_n_temporal
    else:
        is_temporal = has_n_temporal or has_desc_signal

    if not is_temporal:
        logger.debug(
            "Grouping key %s: no temporal signal — not grouping (%d candidates).",
            gkey, len(candidates),
        )
        return None

    # Build phases with correct temporal ordering
    # Priority 1: Known temporal position tag
    # Priority 2: SeriesNumber if distinct and positive
    # Priority 3: Series UID lexicographic sort
    known_positions = {c.series_uid: c.temporal_position for c in candidates if c.temporal_position is not None}
    distinct_snums = len({c.series_number for c in candidates if c.series_number > 0}) == len(candidates)

    if known_positions and len(known_positions) > 1:
        ordered = sorted(candidates, key=lambda c: (c.temporal_position or 9999, c.series_uid))
    elif distinct_snums:
        ordered = sorted(candidates, key=lambda c: (c.series_number, c.series_uid))
    else:
        ordered = sorted(candidates, key=lambda c: c.series_uid)

    distinct_descriptions = len({c.series_description for c in candidates if c.series_description}) > 1
    phases: List[TemporalPhase] = []
    for idx, cand in enumerate(ordered, start=1):
        tp = cand.temporal_position if cand.temporal_position is not None else idx
        label = cand.series_description if (distinct_descriptions and cand.series_description) else f"Phase {idx}"
        phases.append(TemporalPhase(
            series_uid=cand.series_uid,
            temporal_position=tp,
            phase_label=label,
            series_dir=cand.series_dir,
            instance_count=cand.instance_count,
            status="PENDING",
        ))

    # Representative metadata from first candidate
    rep = candidates[0]
    study_uid, for_uid = gkey.split("::", 1)

    # Derive clean group description (common prefix if available)
    common_prefix = os.path.commonprefix([c.series_description for c in candidates]).strip().rstrip("-").rstrip(",").strip()
    group_desc = common_prefix if len(common_prefix) > 3 else rep.series_description

    group = FourDCTGroup(
        group_id=gkey,
        study_instance_uid=study_uid,
        frame_of_reference_uid=for_uid,
        patient_name=rep.patient_name,
        patient_id=rep.patient_id,
        study_date=rep.study_date,
        series_description=group_desc,
        phases=phases,
    )
    group.reference_phase_uid = _select_reference_phase(phases, candidates=candidates)
    return group



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_fourdct_groups(
    series_dirs: Dict[str, str],
) -> Tuple[List[FourDCTGroup], List[str]]:
    """
    Analyse a mapping of {series_uid: directory_path} and partition the
    series into 4DCT groups and plain (ungrouped) series.

    Parameters
    ----------
    series_dirs:
        Mapping of SeriesInstanceUID → absolute path to the series directory.

    Returns
    -------
    groups:
        List of FourDCTGroup objects; each contains ≥ 2 TemporalPhase entries.
    plain_series_uids:
        Series UIDs that were not grouped (plain CT, RTSS, etc.).
    """
    # Step 1: extract candidate metadata from every series directory
    candidates: List[_SeriesCandidate] = []
    plain_uids: List[str] = []

    for series_uid, series_dir in series_dirs.items():
        cand = _extract_candidate(series_uid, series_dir)
        if cand is None:
            # Too few slices, unreadable, or no valid CT image — plain series
            plain_uids.append(series_uid)
        else:
            candidates.append(cand)

    # Step 2: group candidates by grouping key
    by_gkey: Dict[str, List[_SeriesCandidate]] = {}
    ungroupable: List[str] = []

    for cand in candidates:
        if cand.grouping_key is None:
            ungroupable.append(cand.series_uid)
        else:
            by_gkey.setdefault(cand.grouping_key, []).append(cand)

    plain_uids.extend(ungroupable)

    # Step 3: for each grouping-key bucket, attempt to build a FourDCTGroup
    groups: List[FourDCTGroup] = []
    for gkey, bucket in by_gkey.items():
        group = _build_group(gkey, bucket)
        if group is not None:
            logger.info(
                "4DCT group detected: %s (%d phases, ref=%s)",
                gkey, group.phase_count, group.reference_phase_uid,
            )
            groups.append(group)
        else:
            # Not a 4DCT — all series in this bucket remain plain
            for cand in bucket:
                plain_uids.append(cand.series_uid)

    return groups, plain_uids
