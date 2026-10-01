"""Integrity: paediatric protocol match, slice count and slice thickness."""
from typing import Any, Dict, List, Optional

from backend.agents.base import SeriesContext
from backend.models import QAFlag
from backend.qa_config import Thresholds
from backend.status import QAStatus

NAME = "Integrity"


def parse_patient_age_years(age: str) -> Optional[float]:
    """Parse a DICOM Age String (VR AS: nnnY / nnnM / nnnW / nnnD) to years."""
    if not age or len(age) != 4:
        return None
    try:
        value = int(age[:3])
    except ValueError:
        return None
    unit = age[3].upper()
    return {"Y": value, "M": value / 12.0, "W": value / 52.17, "D": value / 365.25}.get(unit)


def compute(ctx: SeriesContext) -> Dict[str, Any]:
    first = ctx.datasets[0]
    protocol = ctx.protocol
    study_desc = ctx.study_desc
    patient_age_str = str(getattr(first, 'PatientAge', ''))
    adult_age = ctx.thresholds.integrity.adult_age_years

    # Both StudyDescription and ProtocolName carry "(Child)" or "(Adult)".
    age_years = parse_patient_age_years(patient_age_str)
    patient_is_child = age_years is not None and age_years < adult_age
    patient_is_adult = age_years is not None and age_years >= adult_age

    study_is_child = "(Child)" in study_desc
    study_is_adult = "(Adult)" in study_desc
    protocol_is_child = "(Child)" in protocol
    protocol_is_adult = "(Adult)" in protocol

    pediatric_mismatch = False
    message = ""

    # Rule 1: patient age vs protocol / study markers
    if patient_is_child and (study_is_adult or protocol_is_adult):
        pediatric_mismatch = True
        message = f"PEDIATRIC_MISMATCH: Child patient ({patient_age_str}) scanned with Adult protocol/study."
    elif patient_is_adult and (study_is_child or protocol_is_child):
        pediatric_mismatch = True
        message = f"PEDIATRIC_MISMATCH: Adult patient ({patient_age_str}) scanned with Child protocol/study."

    # Rule 2: study marker vs protocol marker
    if not pediatric_mismatch and ((study_is_child and protocol_is_adult) or (study_is_adult and protocol_is_child)):
        pediatric_mismatch = True
        message = f"PEDIATRIC_MISMATCH: Protocol '{protocol}' does not match Study Description '{study_desc}'."

    return {
        "series_uid": first.SeriesInstanceUID,
        "patient_name": str(getattr(first, 'PatientName', 'Unknown')),
        "protocol": protocol,
        "slice_count": len(ctx.datasets),
        "slice_thickness": float(first.SliceThickness),
        "pediatric_mismatch": pediatric_mismatch,
        "pediatric_mismatch_message": message,
    }


def evaluate(metrics: Dict[str, Any], t: Thresholds) -> List[QAFlag]:
    flags = []
    if metrics["pediatric_mismatch"]:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=metrics["pediatric_mismatch_message"]))

    if metrics["slice_count"] < t.integrity.min_slice_count:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=f"Insufficient slices for clinical series (Found: {metrics['slice_count']})"))

    thick = t.slice_thickness
    if metrics["slice_thickness"] > thick.absolute_max_mm:
        flags.append(QAFlag(name=NAME, status=QAStatus.REJECT, message=f"Slice thickness exceeds clinical absolute limit ({thick.absolute_max_mm:g}mm)"))
    elif metrics["slice_thickness"] > thick.preferred_max_mm:
        flags.append(QAFlag(name=NAME, status=QAStatus.CONDITIONAL, message=f"Slice thickness exceeds preferred limit ({thick.preferred_max_mm:g}mm)"))
    return flags
