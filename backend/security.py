"""Input validation and request guards for the web API and DICOM listener."""
import ipaddress
import os
import re
from typing import Iterable, List, Optional, Union
from urllib.parse import urlsplit

from fastapi import HTTPException, Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# DICOM UID: digit components separated by dots, max 64 chars (PS3.5 §9.1).
# Leading zeros are tolerated because some scanners emit them; what matters
# here is that a UID can never contain "/", "\\" or "..".
_UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")

# Header every state-changing request from the dashboard carries. Browsers will
# not send a custom header cross-origin without a CORS preflight, which this
# API does not grant, so a third-party page cannot forge these requests.
CSRF_HEADER = "X-RapidCTQA-Request"


def is_valid_uid(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64 and bool(_UID_RE.match(value))


def safe_child_path(base_dir: str, name: str) -> str:
    """Join ``name`` under ``base_dir`` and refuse anything that escapes it."""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, target]) != base or target == base:
        raise ValueError(f"Path {name!r} escapes {base_dir!r}")
    return target


def series_dir(base_dir: str, series_uid: str) -> str:
    """Directory of a series under ``base_dir``; ``series_uid`` must be a valid UID."""
    if not is_valid_uid(series_uid):
        raise ValueError(f"Invalid series UID: {series_uid!r}")
    return safe_child_path(base_dir, series_uid)


def valid_series_uid(series_uid: str = Path(...)) -> str:
    """FastAPI dependency: 400 for anything that is not a DICOM UID."""
    if not is_valid_uid(series_uid):
        raise HTTPException(status_code=400, detail="Invalid series UID")
    return series_uid


Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def parse_networks(entries: Iterable[str]) -> List[Network]:
    return [ipaddress.ip_network(str(e).strip(), strict=False) for e in entries]


def ip_allowed(host: Optional[str], networks: List[Network]) -> bool:
    if host is None:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return any(addr in net for net in networks)


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


class RequestGuardMiddleware(BaseHTTPMiddleware):
    """Client allow-list for everything, CSRF checks for state-changing requests.

    * The client IP must be inside ``allowed_clients``.
    * POST / PUT / PATCH / DELETE must carry :data:`CSRF_HEADER`, and if the
      browser sent an ``Origin`` header it must be this server or one of
      ``allowed_origins``.
    """

    UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app, allowed_clients: Iterable[str], allowed_origins: Iterable[str] = ()):
        super().__init__(app)
        self.networks = parse_networks(allowed_clients)
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else None
        if not ip_allowed(client_host, self.networks):
            return JSONResponse({"detail": "Client not allowed"}, status_code=403)

        if request.method in self.UNSAFE_METHODS:
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"detail": f"Missing {CSRF_HEADER} header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin:
                own_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".lower()
                if _origin_of(origin) not in self.allowed_origins | {own_origin}:
                    return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)

        return await call_next(request)
