from pynetdicom import AE, evt
from pynetdicom.sop_class import CTImageStorage
import os
import pydicom
import threading
from typing import Callable, Dict

class DicomListener:
    STABILITY_SECONDS = 10  # wait this long after the last file before triggering

    def __init__(self, storage_dir: str, callback: Callable[[str], None]):
        self.storage_dir = storage_dir
        self.callback = callback
        self.series_tracker: Dict[str, int] = {}
        self.timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def start(self, host: str = "0.0.0.0", port: int = 11112):
        ae = AE(ae_title="RT_QA_SCP")
        ae.add_supported_context(CTImageStorage)
        
        handlers = [
            (evt.EVT_C_STORE, self._handle_store),
        ]
        
        self.server = ae.start_server((host, port), block=False, evt_handlers=handlers)
        print(f"DICOM Listener started on {host}:{port}")

    def _handle_store(self, event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        
        series_uid = ds.SeriesInstanceUID
        study_dir = os.path.join(self.storage_dir, series_uid)
        os.makedirs(study_dir, exist_ok=True)
        
        filename = os.path.join(study_dir, f"{ds.SOPInstanceUID}.dcm")
        ds.save_as(filename, write_like_original=False)
        
        with self._lock:
            self.series_tracker[series_uid] = self.series_tracker.get(series_uid, 0) + 1
            count = self.series_tracker[series_uid]

            # Reset debounce timer on every incoming file
            if series_uid in self.timers:
                self.timers[series_uid].cancel()

            timer = threading.Timer(
                self.STABILITY_SECONDS,
                self._trigger_callback,
                args=[series_uid],
            )
            self.timers[series_uid] = timer
            timer.start()
            
        if count % 50 == 0:
            print(f"Receiving series {series_uid}: {count} files so far...")

        return 0x0000

    def _trigger_callback(self, series_uid: str):
        with self._lock:
            count = self.series_tracker.pop(series_uid, 0)
            self.timers.pop(series_uid, None)
        print(f"Series {series_uid} stable ({count} files, no new data for {self.STABILITY_SECONDS}s). Triggering analysis...")
        self.callback(series_uid)
