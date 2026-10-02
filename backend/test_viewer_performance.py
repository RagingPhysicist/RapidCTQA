"""DICOM-space mask cache and the fast slice viewer path."""
import glob as glob_module
import io
import os
import threading
import time
from unittest.mock import patch

import nibabel as nib
import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydicom.dataset import Dataset, FileMetaDataset

from backend import mask_cache, settings, state
from backend.segmentation import SegmentationService

D, H, W = 7, 40, 48


# --- legacy implementations (verbatim logic of the previous code) -------------

def legacy_resample(nii_path, datasets):
    nii = nib.load(nii_path)
    nifti_data = nii.get_fdata()
    Dn, Hn, Wn = len(datasets), int(datasets[0].Rows), int(datasets[0].Columns)
    Nx, Ny, Nz = nifti_data.shape[:3]
    inv_affine = np.linalg.inv(nii.affine)
    C, R = np.meshgrid(np.arange(Wn), np.arange(Hn))
    mask_3d = np.zeros((Dn, Hn, Wn), dtype=bool)
    for s in range(Dn):
        ds = datasets[s]
        pos = getattr(ds, 'ImagePositionPatient', [0.0, 0.0, float(s)])
        iop = getattr(ds, 'ImageOrientationPatient', [1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
        spacing = getattr(ds, 'PixelSpacing', [1.0, 1.0])
        dy, dx = float(spacing[0]), float(spacing[1])
        rx, ry, rz = float(iop[0]), float(iop[1]), float(iop[2])
        cx, cy, cz = float(iop[3]), float(iop[4]), float(iop[5])
        X_lps = pos[0] + C * dx * rx + R * dy * cx
        Y_lps = pos[1] + C * dx * ry + R * dy * cy
        Z_lps = pos[2] + C * dx * rz + R * dy * cz
        X_ras, Y_ras, Z_ras = -X_lps, -Y_lps, Z_lps
        I = np.round(inv_affine[0, 0] * X_ras + inv_affine[0, 1] * Y_ras + inv_affine[0, 2] * Z_ras + inv_affine[0, 3]).astype(int)
        J = np.round(inv_affine[1, 0] * X_ras + inv_affine[1, 1] * Y_ras + inv_affine[1, 2] * Z_ras + inv_affine[1, 3]).astype(int)
        K = np.round(inv_affine[2, 0] * X_ras + inv_affine[2, 1] * Y_ras + inv_affine[2, 2] * Z_ras + inv_affine[2, 3]).astype(int)
        valid = (0 <= I) & (I < Nx) & (0 <= J) & (J < Ny) & (0 <= K) & (K < Nz)
        slice_m = np.zeros((Hn, Wn), dtype=bool)
        slice_m[valid] = nifti_data[I[valid], J[valid], K[valid]] > 0
        mask_3d[s] = slice_m
    return mask_3d


def legacy_render(dcm_path, window_width, window_level, metal_threshold, reference_point=None, show_mask=False, slice_mask=None):
    from backend.utils import segment_patient_body_only
    ds = pydicom.dcmread(dcm_path)
    img = ds.pixel_array.astype(np.float32)
    img = img * float(getattr(ds, 'RescaleSlope', 1.0)) + float(getattr(ds, 'RescaleIntercept', 0.0))
    vmin, vmax = window_level - window_width / 2, window_level + window_width / 2
    img_norm = ((np.clip(img, vmin, vmax) - vmin) / (vmax - vmin) * 255).astype(np.uint8)
    rgb = np.stack([img_norm, img_norm, img_norm], axis=-1)
    if show_mask:
        filled = slice_mask if slice_mask is not None and slice_mask.shape == img.shape and np.any(slice_mask) \
            else segment_patient_body_only(img, tissue_threshold_hu=-300)
        if np.any(filled):
            rgb[filled] = (rgb[filled].astype(np.float32) * 0.75 + np.array([50, 150, 250], dtype=np.float32) * 0.25).astype(np.uint8)
    metal_mask = img > metal_threshold
    if np.any(metal_mask):
        rgb[metal_mask] = np.array([220, 50, 50], dtype=np.uint8)
    if reference_point:
        img_z = float(ds.ImagePositionPatient[2])
        if abs(img_z - reference_point['z']) < (float(getattr(ds, 'SliceThickness', 2.0)) / 2.0):
            origin, spacing = ds.ImagePositionPatient, ds.PixelSpacing
            px_x = int((reference_point['x'] - float(origin[0])) / float(spacing[1]))
            px_y = int((reference_point['y'] - float(origin[1])) / float(spacing[0]))
            rows, cols = rgb.shape[0], rgb.shape[1]
            if 0 <= px_x < cols and 0 <= px_y < rows:
                rgb[max(0, px_y - 10):min(rows, px_y + 10), px_x] = [255, 255, 0]
                rgb[px_y, max(0, px_x - 10):min(cols, px_x + 10)] = [255, 255, 0]
    return rgb


# --- synthetic series + NIfTI -------------------------------------------------

def write_series(storage, uid, oblique=False):
    sdir = os.path.join(storage, uid)
    os.makedirs(sdir, exist_ok=True)
    y, x = np.ogrid[:H, :W]
    for i in range(D):
        hu = np.full((H, W), -1000, np.int16)
        hu[((x - 24) / 18.0) ** 2 + ((y - 20) / 13.0) ** 2 <= 1] = 40
        hu[18:21, 22:25] = 3200
        ds = Dataset()
        ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
        ds.SOPInstanceUID = f"{uid}.{i + 1}"
        ds.SeriesInstanceUID = uid
        ds.Modality = "CT"
        ds.Rows, ds.Columns = H, W
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
        ds.PixelRepresentation, ds.SamplesPerPixel = 1, 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelSpacing = [1.3, 0.9]
        ds.SliceThickness = 2.5
        ds.ImagePositionPatient = [-21.7, -18.3, 2.5 * i - 4.0]
        ds.ImageOrientationPatient = [0.996, 0.087, 0.0, -0.087, 0.996, 0.0] if oblique else [1, 0, 0, 0, 1, 0]
        ds.RescaleSlope, ds.RescaleIntercept = 1.0, 0.0
        ds.PixelData = hu.tobytes()
        fm = FileMetaDataset()
        fm.TransferSyntaxUID = "1.2.840.10008.1.2.1"
        fm.MediaStorageSOPClassUID = ds.SOPClassUID
        fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta = fm
        ds.save_as(os.path.join(sdir, f"{i:03d}.dcm"), enforce_file_format=True)
    return sdir


def write_nifti(storage, uid, task="body", seed=0):
    """Random blobby mask with a non-trivial affine: anisotropic, rotated, flipped z, offset."""
    rng = np.random.default_rng(seed)
    data = (rng.random((52, 36, 9)) > 0.55).astype(np.uint8)
    rot = np.array([[0.98, -0.17, 0.0], [0.17, 0.98, 0.0], [0.0, 0.0, 1.0]])
    scale = np.diag([-0.95, -1.25, -2.4])  # all flipped (RAS vs LPS) incl. z
    affine = np.eye(4)
    affine[:3, :3] = rot @ scale
    affine[:3, 3] = [24.0, 22.0, 15.0]
    out = os.path.join(storage, uid, "segmentations", task)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "body.nii.gz")
    nib.save(nib.Nifti1Image(data, affine), path)
    return path


def sorted_headers(sdir):
    dss = [pydicom.dcmread(f, stop_before_pixels=True) for f in glob_module.glob(os.path.join(sdir, "*.dcm"))]
    return sorted(dss, key=lambda d: float(d.ImagePositionPatient[2]))


@pytest.fixture
def series(tmp_path):
    uid = "1.2.826.0.1.3680043.12.1"
    sdir = write_series(str(tmp_path), uid, oblique=True)
    nii = write_nifti(str(tmp_path), uid)
    return SegmentationService(str(tmp_path)), uid, sdir, nii


# --- resampling equality --------------------------------------------------------

@pytest.mark.parametrize("oblique", [False, True])
def test_vectorised_resample_matches_previous_implementation(tmp_path, oblique):
    uid = "1.2.826.0.1.3680043.12.2"
    sdir = write_series(str(tmp_path), uid, oblique=oblique)
    nii = write_nifti(str(tmp_path), uid, seed=3)
    datasets = sorted_headers(sdir)
    expected = legacy_resample(nii, datasets)
    assert expected.any() and not expected.all()

    geometry = mask_cache.geometry_from_datasets(datasets)
    new = mask_cache.resample_to_dicom([mask_cache.load_nifti_mask(nii)], H, W, geometry.slices)
    np.testing.assert_array_equal(new, expected)

    service = SegmentationService(str(tmp_path))
    np.testing.assert_array_equal(service.load_body_mask(uid, datasets=datasets), expected)
    np.testing.assert_array_equal(service.load_body_mask(uid), expected)  # headers read from storage


def test_chunked_resample_equals_single_chunk(series):
    service, uid, sdir, nii = series
    geometry = mask_cache.geometry_from_datasets(sorted_headers(sdir))
    volumes = [mask_cache.load_nifti_mask(nii)]
    whole = mask_cache.resample_to_dicom(volumes, H, W, geometry.slices)
    with patch.object(mask_cache, "_CHUNK_PIXELS", H * W * 2):  # 2 slices per chunk
        np.testing.assert_array_equal(mask_cache.resample_to_dicom(volumes, H, W, geometry.slices), whole)


# --- cache ------------------------------------------------------------------------

def test_cache_created_once_and_read_only(series):
    service, uid, sdir, _ = series
    with patch.object(mask_cache, "resample_to_dicom", wraps=mask_cache.resample_to_dicom) as build:
        mm = service.get_body_mask_dicom_space(uid)
        again = service.get_body_mask_dicom_space(uid)
    assert build.call_count == 1
    assert os.path.exists(os.path.join(sdir, "segmentations", "body", "body_dicom.npy"))
    assert mm.shape == (D, H, W) and mm.dtype == bool
    assert not mm.flags.writeable
    np.testing.assert_array_equal(np.asarray(again), legacy_resample(series[3], sorted_headers(sdir)))


def test_cache_rebuilt_when_nifti_is_newer(series):
    service, uid, sdir, nii = series
    first = np.array(service.get_body_mask_dicom_space(uid))
    cache = service.mask_cache_path(uid)
    write_nifti(os.path.dirname(sdir), uid, seed=7)          # re-segmentation ...
    past = os.stat(nii).st_mtime - 10
    os.utime(cache, (past, past))                            # ... so the NIfTI is newer than the cache
    with patch.object(mask_cache, "resample_to_dicom", wraps=mask_cache.resample_to_dicom) as build:
        second = np.array(service.get_body_mask_dicom_space(uid))
    assert build.call_count == 1
    assert not np.array_equal(first, second)
    np.testing.assert_array_equal(second, legacy_resample(nii, sorted_headers(sdir)))


def test_cache_rebuilt_on_shape_mismatch(series):
    service, uid, sdir, _ = series
    service.get_body_mask_dicom_space(uid)
    with patch.object(mask_cache, "resample_to_dicom", wraps=mask_cache.resample_to_dicom) as build:
        assert service.get_body_mask_dicom_space(uid, expected_shape=(D, H, W)) is not None
        assert build.call_count == 0
        # A series that gained slices no longer matches the cached shape
        datasets = sorted_headers(sdir)
        service.get_body_mask_dicom_space(uid, datasets=datasets[:-1])
        assert build.call_count == 1


def test_concurrent_requests_build_once_atomically(series):
    service, uid, sdir, _ = series
    real = mask_cache.resample_to_dicom

    def slow(*args, **kwargs):
        time.sleep(0.2)
        return real(*args, **kwargs)

    results, errors = [], []

    def worker():
        try:
            results.append(np.array(service.get_body_mask_dicom_space(uid)))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    with patch.object(mask_cache, "resample_to_dicom", side_effect=slow) as build:
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not errors and build.call_count == 1
    assert len(results) == 8 and all(np.array_equal(r, results[0]) for r in results)
    seg_dir = os.path.join(sdir, "segmentations", "body")
    assert sorted(os.listdir(seg_dir)) == ["body.nii.gz", "body_dicom.npy"]  # no temp files left


def test_single_slice_fallback_while_cache_missing(series):
    service, uid, sdir, nii = series
    geometry = mask_cache.geometry_from_datasets(sorted_headers(sdir))
    expected = legacy_resample(nii, sorted_headers(sdir))
    lock = service._series_lock(uid, "body")
    with lock:  # a build is in progress
        with patch.object(mask_cache, "resample_to_dicom", wraps=mask_cache.resample_to_dicom) as resample, \
             patch("threading.Thread") as thread:
            sl = service.get_mask_slice(uid, 3, expected_shape=geometry.shape, slice_geometry=geometry.slices[3])
    np.testing.assert_array_equal(sl, expected[3])
    assert [len(c.args[3]) for c in resample.call_args_list] == [1]  # one slice, not the volume
    thread.assert_not_called()                                        # no second builder
    assert not os.path.exists(service.mask_cache_path(uid))


def test_single_slice_fallback_starts_background_build(series):
    service, uid, sdir, nii = series
    geometry = mask_cache.geometry_from_datasets(sorted_headers(sdir))
    service.get_mask_slice(uid, 0, expected_shape=geometry.shape, slice_geometry=geometry.slices[0])
    deadline = time.time() + 10
    while not os.path.exists(service.mask_cache_path(uid)) and time.time() < deadline:
        time.sleep(0.05)
    np.testing.assert_array_equal(
        service.get_mask_slice(uid, 4, expected_shape=geometry.shape), legacy_resample(nii, sorted_headers(sdir))[4])


def test_load_body_mask_transposes_without_geometry(tmp_path):
    uid = "1.2.826.0.1.3680043.12.3"
    os.makedirs(os.path.join(str(tmp_path), uid))  # no DICOM files
    nii = write_nifti(str(tmp_path), uid)
    data = nib.load(nii).get_fdata() > 0
    mask = SegmentationService(str(tmp_path)).load_body_mask(uid, target_shape=(9, 36, 52))
    np.testing.assert_array_equal(mask, np.transpose(data, (2, 1, 0)))
    assert not os.path.exists(os.path.join(str(tmp_path), uid, "segmentations", "body", "body_dicom.npy"))


# --- slice endpoint ------------------------------------------------------------------

@pytest.fixture
def viewer_series():
    uid = "1.2.826.0.1.3680043.12.9"
    storage = settings.STORAGE_DIR
    sdir = write_series(storage, uid)
    nii = write_nifti(storage, uid)
    state.invalidate_viewer_caches(uid)
    with patch("backend.security.ip_allowed", return_value=True):
        yield TestClient(app_client()), uid, sdir, nii
    state.invalidate_viewer_caches(uid)


def app_client():
    from backend.main import app
    return app


def _png(response):
    return np.asarray(Image.open(io.BytesIO(response.content)).convert("RGB"))


def test_slice_with_mask_needs_no_nifti_or_dicom_glob_once_warm(viewer_series):
    client, uid, sdir, nii = viewer_series
    state.segmentation_service.get_body_mask_dicom_space(uid)  # cache exists (built after segmentation)
    assert client.get(f"/api/viewer/{uid}/slice/2?mask=true").status_code == 200  # warms the series view

    with patch("nibabel.load", wraps=nib.load) as nib_load, \
         patch("glob.glob", wraps=glob_module.glob) as glob_calls, \
         patch("pydicom.dcmread", wraps=pydicom.dcmread) as dcmread:
        for index in (2, 3, 4):
            r = client.get(f"/api/viewer/{uid}/slice/{index}?mask=true&ww=350&wl=50")
            assert r.status_code == 200
    assert nib_load.call_count == 0
    assert glob_calls.call_count == 0
    assert dcmread.call_count == 2  # pixel data of the two not-yet-cached slices only, no headers
    assert "mask;dur=" in r.headers["server-timing"]


def test_slice_output_identical_to_previous_renderer(viewer_series):
    client, uid, sdir, nii = viewer_series
    files = sorted(glob_module.glob(os.path.join(sdir, "*.dcm")))
    ts_mask = legacy_resample(nii, sorted_headers(sdir))
    ref = {"x": -21.7 + 24 * 0.9, "y": -18.3 + 20 * 1.3, "z": 2.5 * 3 - 4.0, "name": "ISO"}
    result = type("R", (), {"metrics": {"reference_point": ref}})()
    with patch.dict(state.results_cache, {uid: result}):
        for index, ww, wl, mask in ((3, 400, 40, "true"), (3, 1500, -600, "false"), (5, 80, 35, "true")):
            r = client.get(f"/api/viewer/{uid}/slice/{index}?ww={ww}&wl={wl}&metal=true&mask={mask}")
            expected = legacy_render(files[index], float(ww), float(wl), 3000.0, ref, mask == "true",
                                     ts_mask[index] if mask == "true" else None)
            np.testing.assert_array_equal(_png(r), expected)


def test_slice_etag_and_cache_headers(viewer_series):
    client, uid, sdir, nii = viewer_series
    url = f"/api/viewer/{uid}/slice/1?mask=true"
    r = client.get(url)
    assert r.headers["cache-control"] == "private, max-age=3600"
    etag = r.headers["etag"]
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 304
    assert client.get(f"/api/viewer/{uid}/slice/1?mask=false").headers["etag"] != etag
    # Re-segmentation (different NIfTI mtime) changes the ETag
    t = os.stat(nii).st_mtime - 10
    os.utime(nii, (t, t))
    assert client.get(url).headers["etag"] != etag


def test_viewer_uses_its_own_pool(viewer_series):
    client, uid, sdir, nii = viewer_series
    assert state.viewer_pool is not state.analysis_pool
    with patch.object(state.analysis_pool, "submit", side_effect=AssertionError("analysis pool used")):
        assert client.get(f"/api/viewer/{uid}/slice/0").status_code == 200
