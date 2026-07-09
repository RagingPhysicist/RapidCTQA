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

class IngestionStatus(BaseModel):
    version: str
    active_transfers: int
    queue_size: int
    processed_today: int
