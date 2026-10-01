import os
import sys
from contextlib import asynccontextmanager

import yaml  # keep at top level: configuration loading depends on it (see AGENTS.md)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

if __package__ in (None, ""):
    # Running main.py directly: make the repository root importable
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import settings, state
from backend.routers import reports, studies, viewer
from backend.security import CSRF_HEADER, RequestGuardMiddleware

# Re-exported for callers and tests that used the old single-module layout
from backend.routers.viewer import approve_series, reject_series  # noqa: F401
from backend.state import results_cache, ct_files_cache, results_cache_lock  # noqa: F401


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Disk and network side effects happen here, never at import time
    settings.ensure_directories()
    print(f"Using storage directory: {settings.STORAGE_DIR}")
    state.cleanup_old_directories()
    state.load_persisted_results()
    state.listener.start(host=settings.DICOM_HOST, port=settings.DICOM_PORT, ae_title=settings.DICOM_AET)
    yield


app = FastAPI(title="RapidCTQA API", lifespan=lifespan)

# The dashboard is served by this app, so it needs no CORS. Only origins
# listed in security.allowed_origins may call the API from another site.
if settings.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", CSRF_HEADER],
    )
app.add_middleware(
    RequestGuardMiddleware,
    allowed_clients=settings.ALLOWED_CLIENTS,
    allowed_origins=settings.ALLOWED_ORIGINS,
)

app.include_router(studies.router)
app.include_router(viewer.router)
app.include_router(reports.router)


@app.get("/")
async def read_index():
    return FileResponse(os.path.join(settings.FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=settings.FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT)
