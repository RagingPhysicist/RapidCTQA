"""Operational settings: ``webApp.yaml``, optional ``webApp.local.yaml``, env vars.

``webApp.yaml`` is committed and holds safe, site-neutral defaults.
``webApp.local.yaml`` (git-ignored) is deep-merged on top of it and holds the
site specifics: storage share, network exposure, allowed DICOM peers.
Environment variables override both:

    RAPIDCTQA_STORAGE_DIR   storage directory for received series
    RAPIDCTQA_LOGS_DIR      daily QA log directory
    RAPIDCTQA_EXPORT_DIR    TPS export directory
    RAPIDCTQA_REPORTS_DIR   PDF report directory
    RAPIDCTQA_CONFIG_LOCAL  path of the local override file

Importing this module has no side effects on disk; directories are created by
:func:`ensure_directories` at application startup.
"""
import copy
import os
from typing import Any, Dict, List

import yaml

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_web_config(root_dir: str = ROOT_DIR) -> Dict[str, Any]:
    with open(os.path.join(root_dir, "webApp.yaml"), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    local_path = os.environ.get("RAPIDCTQA_CONFIG_LOCAL", os.path.join(root_dir, "webApp.local.yaml"))
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            config = _deep_merge(config, yaml.safe_load(f) or {})
    return config


def _exists(path: str) -> bool:
    try:
        return os.path.exists(path) or os.path.exists(path + os.sep)
    except OSError:
        return False


class StorageUnavailableError(RuntimeError):
    pass


def normalise_storage_path(path: str, aliases: List[Dict[str, Any]]) -> str:
    """Resolve a configured storage path through site-specific prefix aliases.

    Each alias is ``{"from": <prefix>, "to": [<candidate roots>]}``. When the
    path starts with ``from``, the first candidate root that exists on this
    machine replaces the prefix. This is how one config can point at the same
    share as a UNC path, a mapped drive letter or a macOS mount point.
    Unmatched paths are returned unchanged. A matched path with no reachable
    candidate raises :class:`StorageUnavailableError`.
    """
    if not path:
        return ""
    unified = path.replace("\\", "/")
    for alias in aliases or []:
        prefix = str(alias.get("from", "")).replace("\\", "/").rstrip("/")
        if not prefix or not unified.lower().startswith(prefix.lower()):
            continue
        rest = unified[len(prefix):]
        for candidate in alias.get("to", []):
            root = str(candidate).rstrip("\\/")
            if _exists(root):
                return os.path.normpath(root.replace("\\", "/") + rest)
        raise StorageUnavailableError(
            f"Storage path {path!r} matches alias {alias.get('from')!r}, but none of "
            f"{alias.get('to')} is reachable from this machine. Mount the share or "
            f"set RAPIDCTQA_STORAGE_DIR.")
    return os.path.normpath(path) if os.name == "nt" else path


config_web = load_web_config()

_backend = config_web.get("backend", {})
_storage_cfg = _backend.get("storage", {}) or {}

STORAGE_ERROR = ""
STORAGE_DIR = os.environ.get("RAPIDCTQA_STORAGE_DIR", "")
if not STORAGE_DIR:
    try:
        STORAGE_DIR = normalise_storage_path(_storage_cfg.get("path", ""), _storage_cfg.get("path_aliases", []))
    except StorageUnavailableError as exc:
        # Reported by ensure_directories() at startup; importing stays side-effect free.
        STORAGE_ERROR = str(exc)
        STORAGE_DIR = _storage_cfg.get("path", "")
if not STORAGE_DIR:
    STORAGE_DIR = os.path.join(ROOT_DIR, "data", "rtct")
elif not os.path.isabs(STORAGE_DIR):
    STORAGE_DIR = os.path.join(ROOT_DIR, STORAGE_DIR)

# Days a received series is kept in STORAGE_DIR before the startup cleanup
# removes it (1 = keep only series modified today). 0 disables the cleanup.
STORAGE_RETENTION_DAYS = int(_storage_cfg.get("retention_days", 1))

EXPORT_DIR = os.environ.get("RAPIDCTQA_EXPORT_DIR") or os.path.join(ROOT_DIR, "TPS_EXPORT")
REPORTS_DIR = os.environ.get("RAPIDCTQA_REPORTS_DIR") or os.path.join(ROOT_DIR, "reports")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
QA_CONFIG_PATH = os.path.join(ROOT_DIR, "ctqa.yaml")

_api_cfg = _backend.get("api", {}) or {}
API_HOST = str(_api_cfg.get("host", "127.0.0.1"))
API_PORT = int(_api_cfg.get("port", 8080))

_listener_cfg = _backend.get("dicom_listener", {}) or {}
DICOM_HOST = str(_listener_cfg.get("host", "0.0.0.0"))
DICOM_PORT = int(_listener_cfg.get("port", 11112))
DICOM_AET = str(_listener_cfg.get("aet", "RT_QA_SCP"))
DICOM_ALLOWED_CALLING_AETS: List[str] = list(_listener_cfg.get("allowed_calling_aets", []) or [])
DICOM_ALLOWED_PEERS: List[str] = list(_listener_cfg.get("allowed_peers", []) or [])
DICOM_STABILITY_SECONDS = float(_listener_cfg.get("stability_seconds", 30))

_security_cfg = config_web.get("security", {}) or {}
# Client networks allowed to reach the web dashboard / API (CIDR or single IP).
ALLOWED_CLIENTS: List[str] = list(_security_cfg.get("allowed_clients", ["127.0.0.1", "::1"]) or [])
# Extra browser origins allowed to call the API cross-origin. The dashboard is
# served from the API itself, so it needs none.
ALLOWED_ORIGINS: List[str] = list(_security_cfg.get("allowed_origins", []) or [])

APP_VERSION = str(config_web.get("app", {}).get("version", "0.0"))


def ensure_directories() -> None:
    if STORAGE_ERROR:
        raise StorageUnavailableError(STORAGE_ERROR)
    for path in (STORAGE_DIR, EXPORT_DIR, REPORTS_DIR):
        os.makedirs(path, exist_ok=True)
