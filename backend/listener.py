from pynetdicom import AE, evt
from pynetdicom.sop_class import CTImageStorage, RTStructureSetStorage
import os
import pydicom
import threading
from typing import Callable, Dict, Iterable, Optional, Set

from backend.security import ip_allowed, is_valid_uid, parse_networks, safe_child_path

RT_STRUCTURE_SET_STORAGE = '1.2.840.10008.5.1.4.1.1.481.3'

# C-STORE failure statuses (PS3.4 Annex B.2.3)
STATUS_SUCCESS = 0x0000
STATUS_REFUSED = 0xA700        # Refused: out of resources (used for policy refusals)
STATUS_CANNOT_UNDERSTAND = 0xC000


class DicomListener:
    """C-STORE SCP that files incoming CT / RTSS objects per series.

    A series is handed to ``callback`` once it is *stable*: no association that
    delivered files for it is still open, and no new file has arrived for
    ``stability_seconds``. Waiting for the association to close means a slow
    sender that pauses mid-transfer is not analysed as a partial series.
    """

    def __init__(
        self,
        storage_dir: str,
        callback: Callable[[str], None],
        stability_seconds: float = 30.0,
        allowed_calling_aets: Iterable[str] = (),
        allowed_peers: Iterable[str] = (),
    ):
        self.storage_dir = storage_dir
        self.callback = callback
        self.stability_seconds = stability_seconds
        self.allowed_calling_aets = [a.strip() for a in allowed_calling_aets if a.strip()]
        self.allowed_peers = parse_networks(allowed_peers)
        self.series_tracker: Dict[str, int] = {}
        self.timers: Dict[str, threading.Timer] = {}
        self._open_assocs: Dict[int, Set[str]] = {}
        self._lock = threading.Lock()

    def start(self, host: str = "0.0.0.0", port: int = 11112, ae_title: str = "RT_QA_SCP"):
        ae = AE(ae_title=ae_title)
        ae.add_supported_context(CTImageStorage)
        ae.add_supported_context(RTStructureSetStorage)
        if self.allowed_calling_aets:
            ae.require_calling_aet = self.allowed_calling_aets
        else:
            print("WARNING: DICOM listener accepts associations from any calling AE title. "
                  "Set backend.dicom_listener.allowed_calling_aets to restrict it.")

        handlers = [
            (evt.EVT_C_STORE, self._handle_store),
            (evt.EVT_RELEASED, self._handle_assoc_closed),
            (evt.EVT_ABORTED, self._handle_assoc_closed),
        ]

        self.server = ae.start_server((host, port), block=False, evt_handlers=handlers)
        print(f"DICOM Listener started on {host}:{port} with AE Title '{ae_title}'")

    def _is_valid_axial_ct(self, ds: pydicom.Dataset) -> bool:
        """
        Filter to only accept transversal CT image slices.
        Reject: Topograms/Scouts (ImageType contains LOCALIZER),
        Dose Reports (Modality != CT or different SOP Class),
        and objects without pixel data.
        """
        # 1. Check for RT Structure Set
        if getattr(ds, 'SOPClassUID', '') == RT_STRUCTURE_SET_STORAGE:
            return True

        # 2. Must be CT modality
        if getattr(ds, 'Modality', '') != 'CT':
            return False

        # 3. Must be CT Image Storage SOP Class
        if getattr(ds, 'SOPClassUID', '') != '1.2.840.10008.5.1.4.1.1.2':
            return False

        # 4. Must NOT be a Localizer (Scout/Topogram)
        image_type = getattr(ds, 'ImageType', [])
        if any('LOCALIZER' in str(t).upper() for t in image_type):
            return False

        # 5. Must have pixel data
        if not hasattr(ds, 'PixelData'):
            return False

        return True

    @staticmethod
    def _referenced_series_uid(ds: pydicom.Dataset) -> Optional[str]:
        """CT series an RT Structure Set refers to (first one found)."""
        try:
            for for_item in getattr(ds, 'ReferencedFrameOfReferenceSequence', []):
                for study_item in getattr(for_item, 'RTReferencedStudySequence', []):
                    for series_item in getattr(study_item, 'RTReferencedSeriesSequence', []):
                        return str(series_item.SeriesInstanceUID)
        except Exception:
            pass
        return None

    def _peer_allowed(self, event) -> bool:
        if not self.allowed_peers:
            return True
        try:
            address = event.assoc.requestor.address
        except AttributeError:
            return False
        return ip_allowed(address, self.allowed_peers)

    def _handle_store(self, event):
        if not self._peer_allowed(event):
            print("Refusing C-STORE from peer outside backend.dicom_listener.allowed_peers")
            return STATUS_REFUSED

        ds = event.dataset
        ds.file_meta = event.file_meta

        if not self._is_valid_axial_ct(ds):
            # Silently ignore non-axial CT files (scouts, reports, etc.)
            return STATUS_SUCCESS

        sop_uid = str(getattr(ds, 'SOPInstanceUID', ''))
        series_uid = str(getattr(ds, 'SeriesInstanceUID', ''))
        if ds.SOPClassUID == RT_STRUCTURE_SET_STORAGE:
            # File the structure set with the CT series it references
            series_uid = self._referenced_series_uid(ds) or series_uid

        # UIDs become directory and file names: only digits and dots allowed
        if not (is_valid_uid(series_uid) and is_valid_uid(sop_uid)):
            print(f"Refusing C-STORE with invalid UID(s): series={series_uid!r} sop={sop_uid!r}")
            return STATUS_CANNOT_UNDERSTAND

        study_dir = safe_child_path(self.storage_dir, series_uid)
        os.makedirs(study_dir, exist_ok=True)
        filename = safe_child_path(study_dir, f"{sop_uid}.dcm")
        ds.save_as(filename, enforce_file_format=True)

        with self._lock:
            self.series_tracker[series_uid] = self.series_tracker.get(series_uid, 0) + 1
            count = self.series_tracker[series_uid]
            self._open_assocs.setdefault(id(event.assoc), set()).add(series_uid)
            self._schedule(series_uid)

        if count % 50 == 0:
            print(f"Receiving series {series_uid}: {count} files so far...")

        return STATUS_SUCCESS

    def _handle_assoc_closed(self, event):
        """Association released or aborted: restart the countdown for its series."""
        with self._lock:
            for series_uid in self._open_assocs.pop(id(event.assoc), set()):
                if series_uid in self.series_tracker:
                    self._schedule(series_uid)

    def _schedule(self, series_uid: str):
        # Caller holds self._lock. Reset the debounce timer for this series.
        if series_uid in self.timers:
            self.timers[series_uid].cancel()
        timer = threading.Timer(self.stability_seconds, self._trigger_callback, args=[series_uid])
        timer.daemon = True
        self.timers[series_uid] = timer
        timer.start()

    def _trigger_callback(self, series_uid: str):
        with self._lock:
            if any(series_uid in s for s in self._open_assocs.values()):
                # Sender still connected: keep waiting rather than analysing a partial series
                self._schedule(series_uid)
                return
            count = self.series_tracker.pop(series_uid, 0)
            self.timers.pop(series_uid, None)
        print(f"Series {series_uid} stable ({count} files, sender disconnected, no new data for {self.stability_seconds:g}s). Triggering analysis...")
        self.callback(series_uid)

    def is_ingesting(self, series_uid: str) -> bool:
        with self._lock:
            return series_uid in self.series_tracker
