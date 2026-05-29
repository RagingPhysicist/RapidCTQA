from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Dict, Any

class QAFlag(BaseModel):
    name: str
    status: str  # ACCEPT, CONDITIONAL, REJECT
    message: Optional[str] = None

class QAResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    series_uid: str
    patient_name: Optional[str] = "Unknown"
    protocol: Optional[str] = "Unknown"
    status: str
    metrics: Dict[str, Any]
    flags: List[QAFlag]
    # Per-slice overlay data for the cockpit viewer (not serialised to JSON).
    # Keys: "metal_masks" -> {slice_idx: np.ndarray bool}, "alignment_points" -> {slice_idx: [(y1,x1),(y2,x2)]}
    overlay_data: Optional[Dict[str, Any]] = None

class StudySummary(BaseModel):
    series_uid: str
    patient_name: Optional[str] = "Unknown"
    patient_id: Optional[str] = "Unknown"
    protocol: Optional[str] = "Unknown"
    study_date: Optional[str] = "Unknown"
    modality: str
    status: str
    instance_count: int

class IngestionStatus(BaseModel):
    active_transfers: int
    queue_size: int
    processed_today: int
