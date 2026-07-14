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

        # Binary opening with a 3x3 structuring element to isolate couch
        opened = ndimage.binary_opening(raw, structure=np.ones((3, 3)))
        labeled_op, num_features_op = ndimage.label(opened)

        couch_mask = np.zeros_like(raw, dtype=bool)
        rows, cols = raw.shape

        if num_features_op > 0:
            for c in range(1, num_features_op + 1):
                comp_mask = (labeled_op == c)
                y_idx, x_idx = np.where(comp_mask)
                if len(y_idx) > 0:
                    ymin, ymax = y_idx.min(), y_idx.max()
                    xmin, xmax = x_idx.min(), x_idx.max()
                    h = ymax - ymin + 1
                    w = xmax - xmin + 1

                    # Spatial heuristic to identify table/couch:
                    # located at bottom, wide, and flat aspect ratio
                    if ymax > rows * 0.75 and w > cols * 0.4 and w > 2.0 * h:
                        # Erase only the bottom portion of this component to prevent erasing patient body/legs
                        y_indices, x_indices = np.where(comp_mask)
                        bottom_indices = y_indices >= int(rows * 0.8)
                        if np.any(bottom_indices):
                            sub_mask = np.zeros_like(comp_mask)
                            sub_mask[y_indices[bottom_indices], x_indices[bottom_indices]] = True
                            couch_mask |= sub_mask

        slice_cleaned = np.copy(slice_data)
        slice_cleaned[couch_mask] = -1000.0

        # 1. Create initial binary tissue mask (includes body, table is pre-cleaned)
        initial_mask = slice_cleaned > tissue_threshold_hu

        # 2. Fill small internal holes (lungs, bowel gas) to make the body a solid mass.
        # This prevents the body from breaking apart during erosion.
        filled_mask = ndimage.binary_fill_holes(initial_mask)

        # 3. Apply Morphological Erosion (2D structure, iterations=3)
        structuring_element = ndimage.generate_binary_structure(2, 1)
        eroded_mask = ndimage.binary_erosion(filled_mask, structure=structuring_element, iterations=3)

        # 4. Perform Connected Component Labeling
        labeled_array, num_features = ndimage.label(eroded_mask)

        target_mask = eroded_mask
        if num_features == 0:
            # Fallback if erosion was too aggressive for a tiny phantom
            labeled_array, num_features = ndimage.label(filled_mask)
            target_mask = filled_mask
            if num_features == 0:
                return np.zeros_like(filled_mask)

        # 5. Find the ID of the largest connected components (the Patient body/legs)
        component_sizes = ndimage.sum(target_mask, labeled_array, range(1, num_features + 1))

        if len(component_sizes) > 0:
            max_size = np.max(component_sizes)
            # Retain components that are at least 15% of the size of the largest component, or are the largest
            significant_labels = [idx + 1 for idx, size in enumerate(component_sizes) if size >= max_size * 0.15]
            body_only_eroded = np.isin(labeled_array, significant_labels)
        else:
            body_only_eroded = np.zeros_like(target_mask)

        # 6. Restore the patient's true skin boundary
        # Dilate the isolated body mask back out by the exact same number of iterations
        final_body_mask = ndimage.binary_dilation(body_only_eroded, structure=structuring_element, iterations=3)

        # 7. Final pass to fill any internal voids left after reconstruction
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
