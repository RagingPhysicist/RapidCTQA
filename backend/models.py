from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class QAFlag(BaseModel):
    name: str
    status: str  # ACCEPT, CONDITIONAL, REJECT
    message: Optional[str] = None

class QAResult(BaseModel):
    series_uid: str
    patient_name: Optional[str] = "Unknown"
    protocol: Optional[str] = "Unknown"
    status: str
    metrics: Dict[str, Any]
    flags: List[QAFlag]

class StudySummary(BaseModel):
    series_uid: str
    patient_name: Optional[str] = "Unknown"
    patient_id: Optional[str] = "Unknown"
    protocol: Optional[str] = "Unknown"
    study_date: Optional[str] = "Unknown"
    modality: str
    status: str
    instance_count: int
    # Discriminator field so the frontend can distinguish from FourDCTSummary
    type: str = "series"

class IngestionStatus(BaseModel):
    version: str
    active_transfers: int
    queue_size: int
    processed_today: int
    totalsegmentator_installed: bool = False

# ---------------------------------------------------------------------------
# 4DCT grouping models
# ---------------------------------------------------------------------------

class TemporalPhaseInfo(BaseModel):
    """One temporal phase within a 4DCT group."""
    series_uid: str
    temporal_position: int
    phase_label: str
    instance_count: int
    status: str = "PENDING"

class FourDCTSummary(BaseModel):
    """
    Logical 4DCT examination composed of ≥ 2 temporal phases.
    Returned by /api/studies alongside plain StudySummary objects.
    """
    type: str = "4dct_group"
    group_id: str                       # StudyInstanceUID::FrameOfReferenceUID
    study_instance_uid: str
    frame_of_reference_uid: str
    patient_name: str = "Unknown"
    patient_id: str = "Unknown"
    study_date: str = "Unknown"
    series_description: str = ""
    phase_count: int
    reference_phase_uid: str
    status: str = "PENDING"
    modality: str = "CT"
    instance_count: int
    phases: List[TemporalPhaseInfo]

class SegmentationRequest(BaseModel):
    """Body of POST /api/viewer/{series_uid}/segment"""
    task: str = "body"
    device: str = "cpu"
    fast: bool = True
    force: bool = False

