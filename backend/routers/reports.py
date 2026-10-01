import glob
import io
import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from backend import state
from backend.logger import export_logs_csv, query_logs
from backend.reporter import generate_pdf_report
from backend.security import valid_series_uid

router = APIRouter(prefix="/api")


@router.get("/logs")
async def get_logs(
    date: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    issue_type: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
):
    """Query logged problem reports and QA results with filters."""
    return query_logs(date_str=date, status=status, issue_type=issue_type, search=search)


@router.get("/logs/download")
async def download_logs(
    date: Optional[str] = Query(default=None, pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
    status: Optional[str] = Query(default=None),
    issue_type: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    format: str = Query(default="json"),
):
    """Download filtered logs as JSON or CSV file."""
    logs = query_logs(date_str=date, status=status, issue_type=issue_type, search=search)
    date_filename = date if date else datetime.now().strftime("%Y-%m-%d")

    if format.lower() == "csv":
        return StreamingResponse(
            io.BytesIO(export_logs_csv(logs).encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=qa_problem_logs_{date_filename}.csv"}
        )

    return StreamingResponse(
        io.BytesIO(json.dumps(logs, indent=2, ensure_ascii=False).encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=qa_problem_logs_{date_filename}.json"}
    )


@router.get("/reports/{series_uid}/pdf")
async def get_pdf_report(series_uid: str = Depends(valid_series_uid)):
    if series_uid not in state.results_cache:
        if glob.glob(os.path.join(state.storage_path(series_uid), "*.dcm")):
            state.on_series_received(series_uid)
    result = state.results_cache.get(series_uid)
    if result is None:
        raise HTTPException(status_code=404, detail="Study not found")

    pdf_path = state.report_path(series_uid)
    try:
        generate_pdf_report(result, pdf_path)
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=f"RapidCTQA_Report_{result.patient_name}_{series_uid[:8]}.pdf"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF Generation failed: {str(e)}")
