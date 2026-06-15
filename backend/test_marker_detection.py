import numpy as np
import pydicom
from backend.engine import QAEngine
from unittest.mock import MagicMock

def test_marker_exclusion():
    # Setup mock engine
    engine = QAEngine("ctqa.yaml")

    # Create 10 slices
    datasets = []
    for i in range(10):
        d = MagicMock(spec=pydicom.Dataset)
        d.PixelSpacing = [1.0, 1.0]
        d.SliceThickness = 2.0
        d.ImagePositionPatient = [0, 0, i * 2]
        d.SeriesInstanceUID = "1.2.3"
        d.RescaleSlope = 1.0
        d.RescaleIntercept = 0.0
        d.PatientName = "Test"
        d.ProtocolName = "Test"
        d.StudyDescription = "Test"
        d.PatientAge = "030Y"
        d.GantryDetectorTilt = 0.0

        pixel_array = np.zeros((100, 100), dtype=np.int16) - 1000
        # Create a "patient" - a circle in the middle
        yy, xx = np.ogrid[:100, :100]
        mask = (xx - 50)**2 + (yy - 50)**2 <= 30**2
        pixel_array[mask] = 0 # Soft tissue

        # Add 3 markers on slice 5
        if i == 5:
            # Anterior (top) - y=20, x=50
            pixel_array[20, 50] = 5000
            # Lateral Left - y=50, x=20
            pixel_array[50, 20] = 5000
            # Lateral Right - y=50, x=80
            pixel_array[50, 80] = 5000

        d.pixel_array = pixel_array
        datasets.append(d)

    metrics = engine._compute_metrics(datasets)

    print(f"Metal Surface CC: {metrics['metal_surface_cc']}")
    print(f"Metal Detected: {metrics['metal_detected']}")
    print(f"Metal Surface Slices: {metrics['metal_surface_slices']}")
    print(f"Marker Detected: {metrics['marker_detected']}")
    print(f"Marker Slices: {metrics['marker_slices']}")

    # Slice 6 (index 5) should be in marker_slices, not in metal_surface_slices
    assert 6 in metrics['marker_slices']
    assert 6 not in metrics['metal_surface_slices']
    assert metrics['marker_detected'] is True
    assert metrics['metal_surface_cc'] == 0.0

if __name__ == "__main__":
    test_marker_exclusion()
