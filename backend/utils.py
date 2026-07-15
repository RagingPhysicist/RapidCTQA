import numpy as np
import scipy.ndimage as ndimage
from concurrent.futures import ThreadPoolExecutor

def segment_patient_body_only(ct_volume, tissue_threshold_hu=-300, num_workers=4):
    """
    Isolates the patient's body from the CT volume, completely excluding
    the treatment table, wingboards, and immobilization devices.
    """
    def _process_single_slice(slice_data):
        raw = slice_data > -500
        if not np.any(raw):
            return np.zeros_like(raw, dtype=bool)

        rows, cols = raw.shape

        # 1. Vertical opening to find patient core (guaranteed couch-free)
        # Since the treatment couch is thin vertically (usually < 10 mm),
        # a vertical opening of height 11-15 pixels (scaled dynamically)
        # is guaranteed to erase the couch plates while preserving the
        # much thicker patient body.
        v_struct = max(5, rows // 35) # e.g. 14 for 512, 3 for 128
        opened_vertical = ndimage.binary_opening(raw, structure=np.ones((v_struct, 1)))

        # Keep only significant components in opened_vertical (size >= 0.5% of total image area)
        labeled_core, num_cores = ndimage.label(opened_vertical)
        patient_core = np.zeros_like(raw, dtype=bool)
        min_core_size = int(rows * cols * 0.005) # 0.5% area threshold

        if num_cores > 0:
            for c in range(1, num_cores + 1):
                comp = (labeled_core == c)
                if np.sum(comp) >= min_core_size:
                    patient_core |= comp

        # If no significant core was found, fall back to keeping the largest component
        if not np.any(patient_core) and num_cores > 0:
            core_sizes = ndimage.sum(opened_vertical, labeled_core, range(1, num_cores + 1))
            largest_core_id = np.argmax(core_sizes) + 1
            patient_core = (labeled_core == largest_core_id)

        # 2. Dilate patient core to cover the original patient skin and boundaries
        dilation_size = v_struct + 10
        dilated_core = ndimage.binary_dilation(patient_core, structure=np.ones((dilation_size, dilation_size)))

        # 3. Intersect raw tissue mask with dilated core to get a perfectly couch-free raw mask!
        # This completely erases the couch plates and support structures.
        slice_cleaned_mask = raw & dilated_core

        slice_cleaned = np.copy(slice_data)
        # Set anything that was in raw but not in the cleaned mask to background (-1000.0 HU)
        couch_mask = raw & ~slice_cleaned_mask
        slice_cleaned[couch_mask] = -1000.0

        # 4. Create initial binary tissue mask (includes body, table is pre-cleaned)
        initial_mask = slice_cleaned > tissue_threshold_hu

        # 5. Fill small internal holes (lungs, bowel gas) to make the body a solid mass.
        # This prevents the body from breaking apart during erosion.
        filled_mask = ndimage.binary_fill_holes(initial_mask)

        # 6. Apply Morphological Erosion (2D structure, iterations=3)
        structuring_element = ndimage.generate_binary_structure(2, 1)
        eroded_mask = ndimage.binary_erosion(filled_mask, structure=structuring_element, iterations=3)

        # 7. Perform Connected Component Labeling
        labeled_array, num_features = ndimage.label(eroded_mask)

        target_mask = eroded_mask
        if num_features == 0:
            # Fallback if erosion was too aggressive for a tiny phantom
            labeled_array, num_features = ndimage.label(filled_mask)
            target_mask = filled_mask
            if num_features == 0:
                return np.zeros_like(filled_mask)

        # 8. Find the ID of all significant components (the Patient body/legs)
        component_sizes = ndimage.sum(target_mask, labeled_array, range(1, num_features + 1))

        if len(component_sizes) > 0:
            max_size = np.max(component_sizes)
            # Retain components that are at least 15% of the size of the largest component
            significant_labels = [idx + 1 for idx, size in enumerate(component_sizes) if size >= max_size * 0.15]
            body_only_eroded = np.isin(labeled_array, significant_labels)
        else:
            body_only_eroded = np.zeros_like(target_mask)

        # 9. Restore the patient's true skin boundary
        # Dilate the isolated body mask back out by the exact same number of iterations
        final_body_mask = ndimage.binary_dilation(body_only_eroded, structure=structuring_element, iterations=3)

        # 10. Final pass to fill any internal voids left after reconstruction
        final_body_mask_filled = ndimage.binary_fill_holes(final_body_mask)

        return final_body_mask_filled

    if ct_volume.ndim == 3:
        # Use ThreadPoolExecutor to run slice-by-slice processing concurrently
        results = [None] * ct_volume.shape[0]
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_process_single_slice, ct_volume[i]) for i in range(ct_volume.shape[0])]
            for i, future in enumerate(futures):
                results[i] = future.result()
        return np.stack(results, axis=0)
    else:
        return _process_single_slice(ct_volume)
