import os
import json
import csv
import io
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

from backend.status import is_actionable, try_normalize_status

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Daily logs contain patient names and IDs; logs/ is git-ignored.
LOGS_DIR = os.environ.get("RAPIDCTQA_LOGS_DIR") or os.path.join(ROOT_DIR, "logs")

_log_lock = threading.Lock()


def get_daily_log_path(date_str: Optional[str] = None) -> str:
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOGS_DIR, f"qa_log_{date_str}.json")


def log_qa_result(
    result: Any,
    patient_id: str = "Unknown",
    timestamp: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Record a QA result in the problem log.

    Only results with an actionable flag (CONDITIONAL / REJECT) are written,
    with just those flags as issues. A result without one removes any earlier
    record for the same series (for example after re-analysis or a 4DCT
    metal downgrade). Returns the written record, or None.
    """
    if timestamp is None:
        ts_dt = datetime.now()
        timestamp = ts_dt.isoformat()
        date_str = ts_dt.strftime("%Y-%m-%d")
    else:
        try:
            ts_dt = datetime.fromisoformat(timestamp)
            date_str = ts_dt.strftime("%Y-%m-%d")
        except Exception:
            date_str = datetime.now().strftime("%Y-%m-%d")

    flags_list = []
    issues = []
    for f in getattr(result, "flags", []):
        f_name = getattr(f, "name", str(f.get("name") if isinstance(f, dict) else ""))
        f_status = getattr(f, "status", str(f.get("status") if isinstance(f, dict) else ""))
        f_msg = getattr(f, "message", str(f.get("message") if isinstance(f, dict) else ""))
        if not is_actionable(f_status):
            continue
        f_status = str(try_normalize_status(f_status))
        flags_list.append({"name": f_name, "status": f_status, "message": f_msg})
        issues.append(f"{f_name}: {f_msg}" if f_msg else f_name)

    series_uid = getattr(result, "series_uid", "")
    status = str(try_normalize_status(getattr(result, "status", "")) or getattr(result, "status", "UNKNOWN"))

    record = None
    if flags_list:
        record = {
            "timestamp": timestamp,
            "date": date_str,
            "series_uid": series_uid,
            "patient_name": getattr(result, "patient_name", "Unknown"),
            "patient_id": patient_id,
            "protocol": getattr(result, "protocol", "Unknown"),
            "status": status,
            "flags": flags_list,
            "metrics": getattr(result, "metrics", {}),
            "issues": issues,
        }

    target_file = get_daily_log_path(date_str)
    with _log_lock:
        os.makedirs(LOGS_DIR, exist_ok=True)
        # Drop earlier records of this series from every daily file, then
        # (re)write it to the target day if it is still a problem.
        for path in _daily_log_files() | {target_file}:
            records = _read_records(path)
            kept = [r for r in records if r.get("series_uid") != series_uid]
            if path == target_file and record is not None:
                kept.append(record)
            if kept != records:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(kept, f, indent=2, ensure_ascii=False)
    return record


def _daily_log_files() -> set:
    if not os.path.isdir(LOGS_DIR):
        return set()
    return {
        os.path.join(LOGS_DIR, name) for name in os.listdir(LOGS_DIR)
        if name.startswith("qa_log_") and name.endswith(".json")
    }


def _read_records(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_all_logs() -> List[Dict[str, Any]]:
    """Retrieve all log records across all daily files."""
    all_records = []
    with _log_lock:
        if os.path.exists(LOGS_DIR):
            for fname in sorted(os.listdir(LOGS_DIR)):
                if fname.startswith("qa_log_") and fname.endswith(".json"):
                    fpath = os.path.join(LOGS_DIR, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if isinstance(data, list):
                                all_records.extend(data)
                    except Exception:
                        pass
    return all_records


def query_logs(
    date_str: Optional[str] = None,
    status: Optional[str] = None,
    issue_type: Optional[str] = None,
    search: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Query logs with filters.
    - date_str: filter by date YYYY-MM-DD
    - status: filter by status (ACCEPT, CONDITIONAL, REJECT; legacy names accepted)
    - issue_type: filter by agent name or substring in flags/issues
    - search: general search on patient name, patient id, or series uid
    """
    records = get_all_logs()
    filtered = []

    for r in records:
        if date_str and date_str.strip():
            if r.get("date") != date_str.strip():
                continue

        if status and status.strip() and status.strip().upper() != "ALL":
            req_status = try_normalize_status(status) or status.strip().upper()
            r_status = try_normalize_status(r.get("status", "")) or r.get("status", "").upper()
            if req_status != r_status:
                continue

        if issue_type and issue_type.strip() and issue_type.strip().upper() != "ALL":
            target_issue = issue_type.strip().lower()
            match_flag = False
            for f in r.get("flags", []):
                fname = f.get("name", "").lower()
                fmsg = f.get("message", "").lower()
                fstat = f.get("status", "").lower()
                if target_issue in fname or target_issue in fmsg or target_issue in fstat:
                    match_flag = True
                    break
            if not match_flag:
                for issue in r.get("issues", []):
                    if target_issue in issue.lower():
                        match_flag = True
                        break
            if not match_flag:
                continue

        if search and search.strip():
            s = search.strip().lower()
            p_name = str(r.get("patient_name", "")).lower()
            p_id = str(r.get("patient_id", "")).lower()
            uid = str(r.get("series_uid", "")).lower()
            proto = str(r.get("protocol", "")).lower()
            if s not in p_name and s not in p_id and s not in uid and s not in proto:
                continue

        filtered.append(r)

    # Sort descending by timestamp
    filtered.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return filtered


def export_logs_csv(records: List[Dict[str, Any]]) -> str:
    """Export list of log records to CSV format string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp", "Date", "Series UID", "Patient Name", "Patient ID",
        "Protocol", "Status", "Issues Count", "Issues"
    ])
    for r in records:
        issues_str = " | ".join(r.get("issues", []))
        writer.writerow([
            r.get("timestamp", ""),
            r.get("date", ""),
            r.get("series_uid", ""),
            r.get("patient_name", ""),
            r.get("patient_id", ""),
            r.get("protocol", ""),
            r.get("status", ""),
            len(r.get("issues", [])),
            issues_str,
        ])
    return output.getvalue()
