from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from typing import Annotated, List, Optional, Dict, Any

from backend.status import QAStatus, normalize_status

# Accepts current and legacy status strings, stores the canonical value.
Status = Annotated[QAStatus, BeforeValidator(normalize_status)]


class _StatusModel(BaseModel):
    model_config = ConfigDict(use_enum_values=True)


class QAFlag(_StatusModel):
    name: str
    status: Status  # ACCEPT, CONDITIONAL, REJECT or SKIPPED
    message: Optional[str] = None

class QAResult(_StatusModel):
    series_uid: str
    patient_name: Optional[str] = "Unknown"
    protocol: Optional[str] = "Unknown"
    status: Status  # ACCEPT, CONDITIONAL or REJECT
    metrics: Dict[str, Any]
    flags: List[QAFlag]

class StudySummary(_StatusModel):
    series_uid: str
    patient_name: Optional[str] = "Unknown"
    patient_id: Optional[str] = "Unknown"
    protocol: Optional[str] = "Unknown"
    study_date: Optional[str] = "Unknown"
    modality: str
    status: Status
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

class TemporalPhaseInfo(_StatusModel):
    """One temporal phase within a 4DCT group."""
    series_uid: str
    temporal_position: int
    phase_label: str
    instance_count: int
    status: Status = QAStatus.PENDING

class FourDCTSummary(_StatusModel):
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
    status: Status = QAStatus.PENDING
    modality: str = "CT"
    instance_count: int
    phases: List[TemporalPhaseInfo]

class SegmentationRequest(BaseModel):
    """Body of POST /api/viewer/{series_uid}/segment"""
    # task becomes a directory name under the series folder: keep it a plain identifier
    task: str = Field(default="body", pattern=r"^[A-Za-z0-9_]{1,64}$")
    device: str = Field(default="cpu", pattern=r"^(cpu|gpu|mps|cuda(:[0-9]+)?)$")
    fast: bool = True
    force: bool = False

