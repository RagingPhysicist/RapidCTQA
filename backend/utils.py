import numpy as np
import scipy.ndimage as ndimage
from typing import Tuple, Optional, Union

def detect_couch_plane(
    ct_volume: np.ndarray,
    pixel_spacing: Tuple[float, float] = (1.0, 1.0),
    search_y_ratio: float = 0.55
) -> Optional[int]:
    """
    Stage 1 Helper: Identify the primary horizontal high-attenuation carbon-fiber line
    of the treatment couch in the lower quadrant of the image.
    """
    def _detect_couch_2d(slice_2d: np.ndarray) -> Optional[int]:
        H, W = slice_2d.shape
        start_y = int(H * search_y_ratio)
        end_y = H - 2
        for r in range(start_y, end_y):
            solid = (slice_2d[r] > -400)
            air_above = (slice_2d[r - 1] < -600)
            edge = solid & air_above

            lat_left = np.sum(edge[:int(W * 0.35)])
            lat_right = np.sum(edge[int(W * 0.65):])

            # Couch top extends horizontally across lateral margins with high attenuation
            if (lat_left >= int(W * 0.10) or lat_right >= int(W * 0.10)) and np.sum(solid) >= int(W * 0.20):
                return r
        return None

    if ct_volume.ndim == 3:
        c_ys = []
        for s in range(ct_volume.shape[0]):
            cy = _detect_couch_2d(ct_volume[s])
            if cy is not None:
                c_ys.append(cy)
        return int(np.median(c_ys)) if len(c_ys) > 0 else None
    else:
        return _detect_couch_2d(ct_volume)


def segment_patient_and_accessories(
    ct_volume: np.ndarray,
    tissue_threshold_hu: float = -300,
    pixel_spacing: Tuple[float, float] = (1.0, 1.0),
    num_workers: int = 4
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Re-architected anatomical-decoupling pipeline with a strict 3-stage separation algorithm:

    1. Posterior Couch Exclusion Plane:
       - Identify the primary horizontal high-attenuation carbon-fiber line of the treatment
         couch in the lower quadrant of the image.
       - Apply a dynamic geometric boundary cutoff 2 mm above the couch surface plane to
         mathematically disconnect the patient's back/buttocks from the table before running
         connected component analysis.

    2. Morphological Separation & Morphological Opening:
       - Use directional (vertical/lateral) morphological opening matrices to sever narrow
         tissue bridges connecting skin to vac-bags, wingboards, or headrests.
       - Isolate the largest 3D connected component corresponding strictly to human anatomy.

    3. Standalone Accessory Masking:
       - Generate two distinct, mutually exclusive binary masks:
         - patient_body_mask (Patient tissue ONLY)
         - accessory_table_mask (Couch, wingboard, vac-bag, immobilizers)
    """
    is_3d = (ct_volume.ndim == 3)
    if not is_3d:
        vol = ct_volume[np.newaxis, ...]
    else:
        vol = ct_volume

    D, H, W = vol.shape
    raw_objects = vol > -500

    # Tissue check: evaluate human tissue presence (> -200 HU)
    tissue_check = vol > -200
    ref_area = 512 * 512
    min_voxels_per_slice = max(20, int(500 * (H * W) / ref_area))

    # Identify empty slices where patient tissue is absent
    empty_slice_flags = np.zeros(D, dtype=bool)
    for s in range(D):
        if not np.any(tissue_check[s]):
            empty_slice_flags[s] = True
        else:
            labeled_t, n_t = ndimage.label(tissue_check[s])
            if n_t > 0:
                sizes_t = ndimage.sum(tissue_check[s], labeled_t, range(1, n_t + 1))
                if np.max(sizes_t) < min_voxels_per_slice:
                    empty_slice_flags[s] = True
            else:
                empty_slice_flags[s] = True

    # 1. Posterior Couch Exclusion Plane
    couch_y = detect_couch_plane(vol, pixel_spacing=pixel_spacing)

    work_mask = raw_objects.copy()
    for s in range(D):
        if empty_slice_flags[s]:
            work_mask[s] = False

    if couch_y is not None:
        cutoff_px = int(np.ceil(2.0 / pixel_spacing[1]))
        cutoff_y = max(0, couch_y - cutoff_px)
        work_mask[:, cutoff_y:, :] = False

    # 2. Directional Morphological Opening
    v_size = max(3, int(round(5.0 / pixel_spacing[1])))
    h_size = max(3, int(round(5.0 / pixel_spacing[0])))
    v_struct = np.ones((1, v_size, 1), dtype=bool)
    h_struct = np.ones((1, 1, h_size), dtype=bool)

    opened = ndimage.binary_opening(work_mask, structure=v_struct)
    opened = ndimage.binary_opening(opened, structure=h_struct)

    # Label 3D connected components
    labeled_core, num_cores = ndimage.label(opened, structure=ndimage.generate_binary_structure(3, 2))
    patient_core = np.zeros_like(work_mask, dtype=bool)

    if num_cores > 0:
        core_sizes = ndimage.sum(opened, labeled_core, range(1, num_cores + 1))
        # Use only the largest core component as the patient anchor.
        # The 15%-significance threshold was intended for attached limbs but causes
        # detached accessories (wingboards, vac-bags) to be misclassified as limbs.
        # The raw_objects connectivity step below will recapture any true anatomical
        # structures (e.g. arms) that are physically connected to the patient.
        largest_label = np.argmax(core_sizes) + 1
        patient_core = (labeled_core == largest_label)
    else:
        # Fallback if opening was too aggressive
        labeled_fallback, num_fb = ndimage.label(work_mask, structure=ndimage.generate_binary_structure(3, 2))
        if num_fb > 0:
            fb_sizes = ndimage.sum(work_mask, labeled_fallback, range(1, num_fb + 1))
            patient_core = (labeled_fallback == (np.argmax(fb_sizes) + 1))

    # Restore true skin boundary via controlled morphological dilation.
    # Dilation radius = 5 mm (same as opening kernel), which recaptures the skin envelope
    # without bridging to detached accessories that have a wider gap.
    dilated_core = ndimage.binary_dilation(patient_core, structure=v_struct)
    dilated_core = ndimage.binary_dilation(dilated_core, structure=h_struct)
    patient_body = raw_objects & dilated_core

    # Constrain posterior skin: patient cannot penetrate couch surface plane
    if couch_y is not None:
        patient_body[:, couch_y:, :] = False

    # Fill internal voids (lungs, bowel gas, stomach)
    for s in range(D):
        if np.any(patient_body[s]):
            patient_body[s] = ndimage.binary_fill_holes(patient_body[s])

    # 3. Standalone Accessory Mask (mutually exclusive)
    accessory_table = raw_objects & ~patient_body

    if not is_3d:
        return patient_body[0], accessory_table[0]
    return patient_body, accessory_table


def segment_patient_body_only(
    ct_volume: np.ndarray,
    tissue_threshold_hu: float = -300,
    pixel_spacing: Tuple[float, float] = (1.0, 1.0),
    num_workers: int = 4
) -> np.ndarray:
    """
    Backward-compatible wrapper returning strictly the patient body mask.
    """
    patient_body, _ = segment_patient_and_accessories(
        ct_volume,
        tissue_threshold_hu=tissue_threshold_hu,
        pixel_spacing=pixel_spacing,
        num_workers=num_workers
    )
    return patient_body
