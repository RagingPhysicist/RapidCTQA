from pynetdicom import AE, evt
from pynetdicom.sop_class import CTImageStorage
import os
import pydicom

STORAGE_DIR = "/data/rtct"

# -----------------------------
# STORE HANDLER
# -----------------------------
def handle_store(event):
    ds = event.dataset
    ds.file_meta = event.file_meta

    series_uid = ds.SeriesInstanceUID
    study_dir = os.path.join(STORAGE_DIR, series_uid)

    os.makedirs(study_dir, exist_ok=True)

    sop_uid = ds.SOPInstanceUID
    filename = os.path.join(study_dir, f"{sop_uid}.dcm")

    ds.save_as(filename, write_like_original=False)

    print(f"Stored: {filename}")

    return 0x0000


# -----------------------------
# SERIES COMPLETION CHECK
# -----------------------------
def check_series_completion(series_dir):
    files = os.listdir(series_dir)
    return len(files)


# -----------------------------
# MAIN AE SETUP
# -----------------------------
ae = AE()

ae.add_supported_context(CTImageStorage)

handlers = [(evt.EVT_C_STORE, handle_store)]

ae.start_server(
    ("0.0.0.0", 11112),
    evt_handlers=handlers,
    block=True
)