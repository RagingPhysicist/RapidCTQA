import numpy as np
from scipy import ndimage
from skimage.measure import inertia_tensor

def calculate_roll_refined(pixel_array, hu_threshold=-300):
    body_mask = pixel_array > hu_threshold
    filled_mask = ndimage.binary_fill_holes(body_mask)

    if not np.any(filled_mask):
        return None

    tensor = inertia_tensor(filled_mask)
    evalues, evectors = np.linalg.eigh(tensor)

    primary_vector = evectors[:, 1]
    angle_rad = np.arctan2(primary_vector[1], primary_vector[0])
    angle_deg = np.degrees(angle_rad)

    normalized_angle = (angle_deg + 45) % 90 - 45
    return normalized_angle

def test_refined_pca():
    # Horizontal ellipse with a "hole" (simulating bowel gas)
    y, x = np.ogrid[:100, :200]
    mask = ((x-100)**2 / 50**2 + (y-50)**2 / 20**2 <= 1)
    # Add a hole
    mask[45:55, 95:105] = False

    # Without hole filling
    tensor_with_hole = inertia_tensor(mask)
    ev_with_hole = np.linalg.eigh(tensor_with_hole)[1][:, 1]
    angle_with_hole = np.degrees(np.arctan2(ev_with_hole[1], ev_with_hole[0]))

    # With hole filling
    angle_filled = calculate_roll_refined(mask)

    print(f"Angle with hole (no filling): {angle_with_hole:.4f}")
    print(f"Angle with hole (filled): {angle_filled:.4f}")

    # Twist detection test (derivative based)
    z_coords = [0, 10, 20, 30] # in mm
    roll_angles = [0, 1.1, 2.2, 3.3] # 1.1 deg per 10mm = 1.1 deg per 1cm
    yaw_drift_limit = 1.0 # deg/cm

    def detect_twist_refined(z_coords, roll_angles, limit):
        twist_detected = False
        for i in range(len(roll_angles) - 1):
            dist_cm = abs(z_coords[i+1] - z_coords[i]) / 10.0
            if dist_cm > 0:
                drift = abs(roll_angles[i+1] - roll_angles[i]) / dist_cm
                if drift > limit:
                    twist_detected = True
        return twist_detected

    print(f"Twist detected (>1.0 deg/cm): {detect_twist_refined(z_coords, roll_angles, yaw_drift_limit)}")

if __name__ == "__main__":
    test_refined_pca()
