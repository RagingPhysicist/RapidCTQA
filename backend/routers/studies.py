import glob
import os
from typing import Any, Dict, List

import pydicom
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from backend import settings, state
from backend.engine import CT_IMAGE_STORAGE
from backend.fourdct import FourDCTGroup, detect_fourdct_groups
from backend.models import FourDCTSummary, IngestionStatus, QAResult, StudySummary, TemporalPhaseInfo
from backend.security import valid_series_uid
from backend.status import QAStatus, severity, worst_status

router = APIRouter(prefix="/api")


@router.get("/status", response_model=IngestionStatus)
async def get_status():
    return IngestionStatus(
        version=settings.APP_VERSION,
        active_transfers=0,
        queue_size=len(state.results_cache),
        processed_today=len(state.results_cache),
        totalsegmentator_installed=state.segmentation_service.is_available,
    )


def _read_series_metadata(files: List[str], patient_name: str, protocol: str) -> Dict[str, str]:
    """Patient / protocol metadata from the first readable CT file of a series."""
    meta = {"patient_name": patient_name, "protocol": protocol, "patient_id": "Unknown", "study_date": "Unknown"}

    def take(ds):
        meta["patient_name"] = str(getattr(ds, 'PatientName', patient_name))
        meta["protocol"] = str(getattr(ds, 'ProtocolName', protocol))
        meta["patient_id"] = str(getattr(ds, 'PatientID', 'Unknown'))
        meta["study_date"] = str(getattr(ds, 'StudyDate', 'Unknown'))

    for f in sorted(files):
        try:
            ds = pydicom.dcmread(f, stop_before_pixels=True)
            if getattr(ds, 'SOPClassUID', '') == CT_IMAGE_STORAGE:
                take(ds)
                return meta
        except Exception:
            continue
    try:
        take(pydicom.dcmread(files[0], stop_before_pixels=True))
    except Exception:
        pass
    return meta


def _phase_infos(group: FourDCTGroup, background_tasks: BackgroundTasks = None) -> List[TemporalPhaseInfo]:
    infos = []
    for phase in group.phases:
        status = state.series_status(phase.series_uid)
        if status == QAStatus.PENDING and background_tasks is not None:
            background_tasks.add_task(state.on_series_received, phase.series_uid)
        infos.append(TemporalPhaseInfo(
            series_uid=phase.series_uid,
            temporal_position=phase.temporal_position,
            phase_label=phase.phase_label,
            instance_count=phase.instance_count,
            status=status,
        ))
    return infos


def _group_summary(group: FourDCTGroup, phases: List[TemporalPhaseInfo], status) -> FourDCTSummary:
    return FourDCTSummary(
        group_id=group.group_id,
        study_instance_uid=group.study_instance_uid,
        frame_of_reference_uid=group.frame_of_reference_uid,
        patient_name=group.patient_name,
        patient_id=group.patient_id,
        study_date=group.study_date,
        series_description=group.series_description,
        phase_count=group.phase_count,
        reference_phase_uid=group.reference_phase_uid,
        status=status,
        modality="CT",
        instance_count=group.instance_count,
        phases=phases,
    )


@router.get("/studies")
async def get_studies(background_tasks: BackgroundTasks) -> List[Any]:
    """
    Return a list of study summaries, most severe first.

    Each item is one of:
      - {"type": "series", ...}     - a plain CT series (StudySummary)
      - {"type": "4dct_group", ...} - a logical 4DCT group (FourDCTSummary)
    """
    series_dirs: Dict[str, str] = {}
    raw_summaries: Dict[str, StudySummary] = {}

    for series_uid in state.list_series_dirs():
        study_path = state.storage_path(series_uid)
        files = glob.glob(os.path.join(study_path, "*.dcm"))
        if not files:
            continue
        series_dirs[series_uid] = study_path

        cached_res = state.results_cache.get(series_uid)
        meta = {"patient_name": "Unknown", "protocol": "Unknown", "patient_id": "Unknown", "study_date": "Unknown"}
        if cached_res:
            meta["patient_name"] = cached_res.patient_name
            meta["protocol"] = cached_res.protocol
        # patient_id / study_date are not part of the QA result, so always read them
        meta = _read_series_metadata(files, meta["patient_name"], meta["protocol"])

        status = state.series_status(series_uid)
        if status == QAStatus.PENDING:
            background_tasks.add_task(state.on_series_received, series_uid)

        raw_summaries[series_uid] = StudySummary(
            series_uid=series_uid,
            modality="CT",
            status=status,
            instance_count=len(files),
            **meta,
        )

    groups, plain_uids = detect_fourdct_groups(series_dirs)
    # Phases analysed before their group was complete get the group metal policy now
    state.apply_group_metal_policy(groups)
    with state.fourdct_cache_lock:
        state.fourdct_cache.clear()
        for g in groups:
            state.fourdct_cache[g.group_id] = g

    result_items: List[Any] = [raw_summaries[uid] for uid in plain_uids if uid in raw_summaries]
    for group in groups:
        phases = _phase_infos(group, background_tasks)
        # Worst phase status wins
        result_items.append(_group_summary(group, phases, worst_status(p.status for p in phases)))

    result_items.sort(key=lambda item: severity(item.status))
    return [item.model_dump(mode="json") for item in result_items]


@router.get("/studies/{series_uid}", response_model=QAResult)
async def get_study_detail(series_uid: str = Depends(valid_series_uid)):
    if series_uid not in state.results_cache:
        # Run validation if files exist but no result is cached yet
        if glob.glob(os.path.join(state.storage_path(series_uid), "*.dcm")):
            state.on_series_received(series_uid)

    if series_uid in state.results_cache:
        return state.results_cache[series_uid]
    raise HTTPException(status_code=404, detail="Study not found or not yet processed")


@router.get("/studies/group/{group_id:path}")
async def get_group_detail(group_id: str):
    """Full FourDCTSummary for a detected 4DCT group including per-phase QA status."""
    with state.fourdct_cache_lock:
        group = state.fourdct_cache.get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="4DCT group not found. Refresh the study list first.")
    return _group_summary(group, _phase_infos(group), QAStatus.PENDING).model_dump(mode="json")


@router.post("/validate/{series_uid}")
async def run_validation(background_tasks: BackgroundTasks, series_uid: str = Depends(valid_series_uid)):
    background_tasks.add_task(state.on_series_received, series_uid)
    return {"message": "Validation triggered"}
