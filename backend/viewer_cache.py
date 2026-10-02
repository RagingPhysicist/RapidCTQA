"""Per-series caches and the fast PNG renderer behind the slice viewer.

A slice request should touch no DICOM headers and no NIfTI files once warm:
  * SeriesView      - sorted CT file list plus per-slice geometry, read once per series
  * SliceLRU        - decoded HU slices (int16 when exact, else float32), keyed by (series, index)
  * render_slice_png - window via a cached uint8 lookup table, PNG with compress_level=1;
                       pixel output identical to the previous renderer
"""
from __future__ import annotations

import io
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import numpy as np
import pydicom
from PIL import Image

from backend.mask_cache import SeriesGeometry, slice_geometry
from backend.utils import segment_patient_body_only


@dataclass
class SeriesView:
    files: List[str]
    geometry: SeriesGeometry
    slice_thickness: List[float]          # per slice, default 2.0 (crosshair tolerance)

    @classmethod
    def from_headers(cls, headers: List[Tuple[str, pydicom.Dataset]]) -> "SeriesView":
        """``headers``: (path, dataset read with stop_before_pixels) sorted by z."""
        datasets = [ds for _, ds in headers]
        return cls(
            files=[path for path, _ in headers],
            geometry=SeriesGeometry(
                rows=int(getattr(datasets[0], 'Rows', 512)) if datasets else 0,
                columns=int(getattr(datasets[0], 'Columns', 512)) if datasets else 0,
                slices=tuple(slice_geometry(ds, i) for i, ds in enumerate(datasets)),
            ),
            slice_thickness=[float(getattr(ds, 'SliceThickness', 2.0)) for ds in datasets],
        )

    def crosshair(self, index: int, reference_point: Optional[dict]) -> Optional[Tuple[int, int]]:
        """Pixel (x, y) of the reference point if it lies on this slice."""
        if not reference_point:
            return None
        try:
            geo = self.geometry.slices[index]
            if abs(geo.position[2] - reference_point['z']) >= self.slice_thickness[index] / 2.0:
                return None
            px_x = int((reference_point['x'] - geo.position[0]) / geo.spacing[1])
            px_y = int((reference_point['y'] - geo.position[1]) / geo.spacing[0])
            return px_x, px_y
        except (KeyError, IndexError, TypeError, ZeroDivisionError):
            return None


def decode_hu(path: str) -> np.ndarray:
    """HU slice exactly as the renderer computes it, stored compactly."""
    ds = pydicom.dcmread(path)
    img = ds.pixel_array.astype(np.float32)
    img = img * float(getattr(ds, 'RescaleSlope', 1.0)) + float(getattr(ds, 'RescaleIntercept', 0.0))
    if np.all(np.isfinite(img)) and img.min() >= -32768 and img.max() <= 32767 and np.array_equal(img, np.round(img)):
        return img.astype(np.int16)
    return img


@dataclass
class _SliceEntry:
    hu: np.ndarray
    rule_mask: Optional[np.ndarray] = None   # rule-based body mask, computed on first use


class SliceLRU:
    def __init__(self, max_slices: int = 160):
        self.max_slices = max_slices
        self._items: "OrderedDict[Tuple[str, int], _SliceEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, series_uid: str, index: int, path: str) -> _SliceEntry:
        key = (series_uid, index)
        with self._lock:
            entry = self._items.get(key)
            if entry is not None:
                self._items.move_to_end(key)
                return entry
        entry = _SliceEntry(decode_hu(path))
        with self._lock:
            self._items[key] = entry
            while len(self._items) > self.max_slices:
                self._items.popitem(last=False)
        return entry

    def invalidate(self, series_uid: str) -> None:
        with self._lock:
            for key in [k for k in self._items if k[0] == series_uid]:
                del self._items[key]

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._items


@lru_cache(maxsize=32)
def window_lut(window_width: float, window_level: float) -> np.ndarray:
    """uint8 value for every int16 HU, same float32 arithmetic as the direct formula."""
    return _window(np.arange(-32768, 32768, dtype=np.float32), window_width, window_level)


def _window(img: np.ndarray, window_width: float, window_level: float) -> np.ndarray:
    vmin = window_level - window_width / 2
    vmax = window_level + window_width / 2
    return ((np.clip(img, vmin, vmax) - vmin) / (vmax - vmin) * 255).astype(np.uint8)


def body_mask_for(entry: _SliceEntry) -> np.ndarray:
    if entry.rule_mask is None:
        entry.rule_mask = segment_patient_body_only(entry.hu.astype(np.float32), tissue_threshold_hu=-300)
    return entry.rule_mask


def render_slice_png(
    entry: _SliceEntry,
    window_width: float,
    window_level: float,
    metal_threshold: float,
    crosshair: Optional[Tuple[int, int]] = None,
    show_mask: bool = False,
    slice_mask: Optional[np.ndarray] = None,
) -> bytes:
    """PNG of one slice with W/L, optional body-mask tint, metal overlay and crosshair."""
    hu = entry.hu
    if hu.dtype == np.int16:
        gray = window_lut(float(window_width), float(window_level))[hu.astype(np.int32) + 32768]
    else:
        gray = _window(hu, window_width, window_level)
    rgb = np.stack([gray, gray, gray], axis=-1)

    # Patient mask: light blue tint (TotalSegmentator mask when available, rule-based otherwise)
    if show_mask:
        try:
            if slice_mask is not None and slice_mask.shape == hu.shape and np.any(slice_mask):
                filled = slice_mask
            else:
                filled = body_mask_for(entry)
            if np.any(filled):
                rgb[filled] = (rgb[filled].astype(np.float32) * 0.75 + np.array([50, 150, 250], dtype=np.float32) * 0.25).astype(np.uint8)
        except Exception as e:
            print(f"Error drawing patient mask: {e}")

    # Metal overlay: pixels above threshold -> red
    metal_mask = hu > metal_threshold
    if np.any(metal_mask):
        rgb[metal_mask] = np.array([220, 50, 50], dtype=np.uint8)

    # Reference point crosshair (yellow, 10 px arms)
    if crosshair is not None:
        px_x, px_y = crosshair
        rows, cols = rgb.shape[0], rgb.shape[1]
        if 0 <= px_x < cols and 0 <= px_y < rows:
            size = 10
            rgb[max(0, px_y - size):min(rows, px_y + size), px_x] = [255, 255, 0]
            rgb[px_y, max(0, px_x - size):min(cols, px_x + size)] = [255, 255, 0]

    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format='PNG', compress_level=1)
    return buf.getvalue()
