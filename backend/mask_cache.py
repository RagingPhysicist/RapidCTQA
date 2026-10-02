"""Resampling of TotalSegmentator NIfTI masks into DICOM voxel space, and the
persisted per-series cache used by the QA engine and the slice viewer.

Cache file: ``<storage>/<series_uid>/segmentations/<task>/body_dicom.npy``,
a (slices, rows, columns) bool array in the order of the series' CT images
sorted by z. It is rebuilt when any source NIfTI is newer than the cache or
the expected shape changes, and written atomically (temp file + os.replace).
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

CACHE_FILENAME = "body_dicom.npy"
_CHUNK_PIXELS = 2_000_000  # slices per resampling chunk ~ this many pixels (bounds memory)


@dataclass(frozen=True)
class SliceGeometry:
    """Per-slice DICOM geometry needed to map pixels to patient coordinates."""
    position: Tuple[float, float, float]     # ImagePositionPatient
    orientation: Tuple[float, ...]           # ImageOrientationPatient (6)
    spacing: Tuple[float, float]             # PixelSpacing (row, column)


@dataclass(frozen=True)
class SeriesGeometry:
    rows: int
    columns: int
    slices: Tuple[SliceGeometry, ...]

    @property
    def shape(self) -> Tuple[int, int, int]:
        return (len(self.slices), self.rows, self.columns)


def slice_geometry(ds, index: int) -> SliceGeometry:
    pos = getattr(ds, 'ImagePositionPatient', [0.0, 0.0, float(index)])
    iop = getattr(ds, 'ImageOrientationPatient', [1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
    spacing = getattr(ds, 'PixelSpacing', [1.0, 1.0])
    return SliceGeometry(tuple(float(v) for v in pos), tuple(float(v) for v in iop),
                         (float(spacing[0]), float(spacing[1])))


def geometry_from_datasets(datasets: Sequence) -> SeriesGeometry:
    """Geometry of CT datasets already sorted by z."""
    return SeriesGeometry(
        rows=int(getattr(datasets[0], 'Rows', 512)),
        columns=int(getattr(datasets[0], 'Columns', 512)),
        slices=tuple(slice_geometry(ds, s) for s, ds in enumerate(datasets)),
    )


def load_nifti_mask(path: str) -> Tuple[np.ndarray, np.ndarray]:
    """(bool voxel array, inverse affine) without converting the data to float64."""
    import nibabel as nib
    nii = nib.load(path)
    data = np.asanyarray(nii.dataobj)
    return (data > 0), np.linalg.inv(nii.affine)


def resample_to_dicom(volumes: Sequence[Tuple[np.ndarray, np.ndarray]], rows: int, columns: int,
                      slices: Sequence[SliceGeometry]) -> np.ndarray:
    """Nearest-neighbour resample of NIfTI masks (RAS) onto DICOM slices (LPS).

    Vectorised over chunks of slices; numerically identical to the previous
    per-slice loop (same float64 expression order, np.round, OR across masks).
    """
    out = np.zeros((len(slices), rows, columns), dtype=bool)
    if not slices:
        return out
    C, R = np.meshgrid(np.arange(columns), np.arange(rows))
    C, R = C[None], R[None]
    chunk = max(1, _CHUNK_PIXELS // (rows * columns))

    for start in range(0, len(slices), chunk):
        geo = slices[start:start + chunk]
        col = lambda values: np.array(values, dtype=np.float64)[:, None, None]
        p0, p1, p2 = (col([g.position[a] for g in geo]) for a in range(3))
        rx, ry, rz, cx, cy, cz = (col([g.orientation[a] for g in geo]) for a in range(6))
        dy, dx = col([g.spacing[0] for g in geo]), col([g.spacing[1] for g in geo])

        x_ras = -(p0 + C * dx * rx + R * dy * cx)
        y_ras = -(p1 + C * dx * ry + R * dy * cy)
        z_ras = p2 + C * dx * rz + R * dy * cz

        block = np.zeros((len(geo), rows, columns), dtype=bool)
        for data, inv in volumes:
            if data.ndim != 3:
                continue
            nx, ny, nz = data.shape[:3]
            i = np.round(inv[0, 0] * x_ras + inv[0, 1] * y_ras + inv[0, 2] * z_ras + inv[0, 3]).astype(np.int32)
            j = np.round(inv[1, 0] * x_ras + inv[1, 1] * y_ras + inv[1, 2] * z_ras + inv[1, 3]).astype(np.int32)
            k = np.round(inv[2, 0] * x_ras + inv[2, 1] * y_ras + inv[2, 2] * z_ras + inv[2, 3]).astype(np.int32)
            valid = (0 <= i) & (i < nx) & (0 <= j) & (j < ny) & (0 <= k) & (k < nz)
            block[valid] |= data[i[valid], j[valid], k[valid]]
        out[start:start + len(geo)] = block
    return out


def write_atomic(path: str, mask: np.ndarray) -> None:
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(tmp, "wb") as f:
            np.save(f, np.ascontiguousarray(mask, dtype=bool))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def cache_is_fresh(path: str, sources: Sequence[str], expected_shape: Optional[Tuple[int, ...]] = None) -> bool:
    try:
        cache_mtime = os.stat(path).st_mtime
        if any(os.stat(s).st_mtime > cache_mtime for s in sources):
            return False
        if expected_shape is not None:
            return tuple(np.load(path, mmap_mode="r").shape) == tuple(expected_shape)
        return True
    except (OSError, ValueError):
        return False


class NiftiLRU:
    """Small in-memory cache of loaded NIfTI masks for single-slice fallbacks."""

    def __init__(self, max_entries: int = 2):
        self.max_entries = max_entries
        self._items: "OrderedDict[tuple, List[Tuple[np.ndarray, np.ndarray]]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sources: Sequence[str]) -> List[Tuple[np.ndarray, np.ndarray]]:
        key = tuple((s, os.stat(s).st_mtime) for s in sources)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        volumes = [load_nifti_mask(s) for s in sources]
        with self._lock:
            self._items[key] = volumes
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return volumes
