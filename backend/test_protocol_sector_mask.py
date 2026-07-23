import unittest
from unittest.mock import MagicMock
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
import numpy as np
import os
import shutil
from backend.engine import QAEngine
from backend.reporter import generate_pdf_report

class TestProtocolSectorMask(unittest.TestCase):
    def setUp(self):
        # Create a minimal config for QAEngine
        self.config_path = "test_ctqa_sector.yaml"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("""
thresholds:
  implants:
    metal_threshold_hu: 2000
    max_volume_cc: 0.05
  alignment:
    hu_floor: -300
    angular_step_deg: 0.1
    max_allowable_tilt_deg: 1.5
""")
        self.engine = QAEngine(self.config_path)
        self.test_dir = "test_data_sector"
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def create_ct_series(self, protocol, study_desc="", body_part="", num_slices=5, pixel_spacing=[1.0, 1.0]):
        """Helper to create a list of DICOM file paths forming a test series."""
        paths = []
        for i in range(num_slices):
            ds = Dataset()
            ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.2'  # CT Image Storage
            ds.SOPInstanceUID = f"1.2.3.4.{i}"
            ds.SeriesInstanceUID = "1.2.3.4"
            ds.PatientName = "TestPatient"
            ds.ProtocolName = protocol
            ds.StudyDescription = study_desc
            ds.BodyPartExamined = body_part
            
            ds.Rows = 128
            ds.Columns = 128
            ds.BitsAllocated = 16
            ds.BitsStored = 12
            ds.HighBit = 11
            ds.PixelRepresentation = 0
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.PixelSpacing = pixel_spacing
            ds.SliceThickness = 2.0
            ds.ImagePositionPatient = [0.0, 0.0, float(i * 2)]
            ds.RescaleSlope = 1.0
            ds.RescaleIntercept = -1024.0

            # Default to background air (-1000 HU -> stored as 24)
            pixels = np.ones((128, 128), dtype=np.uint16) * 24
            ds.PixelData = pixels.tobytes()

            file_meta = FileMetaDataset()
            file_meta.TransferSyntaxUID = '1.2.840.10008.1.2.1'
            file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
            file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
            ds.file_meta = file_meta

            path = os.path.join(self.test_dir, f"slice_{i}.dcm")
            ds.save_as(path, write_like_original=False)
            paths.append(path)
        return paths

    def test_protocol_group_mapping(self):
        # We can verify mapping behaviour by analyzing series with clean/air volumes
        # and checking the mapped depths or behaviour. Since we don't expose depths directly,
        # we can verify it indirectly via rejection/acceptance of specific truncation.
        pass

    def test_lateral_truncation_tolerances(self):
        # Create a series with a simulated lateral truncation (left lateral edge) of 10 mm depth
        # We set a block of size 5x10 pixels at the left edge to -100 HU (body pixel -> stored as 924)
        # Left Lateral: row 62 to 66 (5 rows), col 0 to 9 (10 cols)
        
        # 1. Test Breast/Thorax protocol (15 mm tolerance) -> Should accept
        paths_breast = self.create_ct_series(protocol="Breast Wingboard Scan", study_desc="Thorax Study")
        # Load one slice, modify its pixels, save it back
        ds = pydicom.dcmread(paths_breast[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[62:67, 0:10] = 924 # -100 HU
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_breast[2], write_like_original=False)

        result_breast = self.engine.analyze_series(paths_breast)
        # Check if TRUNCATION_ERROR is NOT in flags
        truncation_flags = [f for f in result_breast.flags if "TRUNCATION_ERROR" in f.message]
        self.assertEqual(len(truncation_flags), 0, "Breast scan should tolerate 10mm lateral truncation")

        # Verify metrics for tolerated truncation
        self.assertIn("tolerated_truncated_slices", result_breast.metrics)
        self.assertEqual(result_breast.metrics["tolerated_truncated_slices"], [3])
        self.assertEqual(result_breast.metrics["truncated_slices"], [])
        self.assertTrue(result_breast.metrics["truncation_detected"])
        self.assertFalse(result_breast.metrics["truncation_error"])

        # 2. Test Head/Neck protocol (5 mm tolerance) -> Should reject
        paths_hn = self.create_ct_series(protocol="H&N C-Spine", study_desc="Brain Study")
        ds = pydicom.dcmread(paths_hn[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[62:67, 0:10] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_hn[2], write_like_original=False)

        result_hn = self.engine.analyze_series(paths_hn)
        truncation_flags_hn = [f for f in result_hn.flags if "TRUNCATION_ERROR" in f.message]
        self.assertGreater(len(truncation_flags_hn), 0, "H&N scan should NOT tolerate 10mm lateral truncation")
        self.assertEqual(result_hn.metrics["truncated_slices"], [3])
        self.assertEqual(result_hn.metrics["tolerated_truncated_slices"], [])
        self.assertTrue(result_hn.metrics["truncation_detected"])
        self.assertTrue(result_hn.metrics["truncation_error"])

        # 3. Test Pelvis/Prostate protocol (0 mm tolerance) -> Should reject
        paths_pelvis = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study")
        ds = pydicom.dcmread(paths_pelvis[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[62:67, 0:10] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_pelvis[2], write_like_original=False)

        result_pelvis = self.engine.analyze_series(paths_pelvis)
        truncation_flags_pelvis = [f for f in result_pelvis.flags if "TRUNCATION_ERROR" in f.message]
        self.assertGreater(len(truncation_flags_pelvis), 0, "Pelvis scan should NOT tolerate 10mm lateral truncation")
        self.assertEqual(result_pelvis.metrics["truncated_slices"], [3])
        self.assertEqual(result_pelvis.metrics["tolerated_truncated_slices"], [])
        self.assertTrue(result_pelvis.metrics["truncation_detected"])
        self.assertTrue(result_pelvis.metrics["truncation_error"])

    def test_generate_pdf_report_with_tolerated_truncation(self):
        paths_breast = self.create_ct_series(protocol="Breast Wingboard Scan", study_desc="Thorax Study")
        ds = pydicom.dcmread(paths_breast[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[62:67, 0:10] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_breast[2], write_like_original=False)

        result_breast = self.engine.analyze_series(paths_breast)

        # Output path for test PDF report
        report_path = os.path.join(self.test_dir, "test_report_breast.pdf")

        # Verify PDF report generation compiles without error
        generate_pdf_report(result_breast, report_path)
        self.assertTrue(os.path.exists(report_path))

    def test_head_neck_posterior_table_exclusion(self):
        # Create a simulated table contact at the posterior edge (bottom: row 125 to 127)
        # Bottom edge: rows 125 to 127 (3 rows), cols 60 to 68 (9 cols) -> 27 pixels
        # Head & Neck should ignore this table contact and accept, while Pelvis should reject it.

        # 1. H&N Scan -> Should accept posterior edge table contact
        paths_hn = self.create_ct_series(protocol="Head scan", study_desc="Neck C-Spine")
        ds = pydicom.dcmread(paths_hn[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[125:128, 60:69] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_hn[2], write_like_original=False)

        result_hn = self.engine.analyze_series(paths_hn)
        truncation_flags_hn = [f for f in result_hn.flags if "TRUNCATION_ERROR" in f.message]
        self.assertEqual(len(truncation_flags_hn), 0, "H&N scan should ignore posterior table contact")

        # 2. Pelvis Scan -> Should reject posterior edge table contact (strict 0mm)
        paths_pelvis = self.create_ct_series(protocol="Pelvis scan", study_desc="Pelvis Study")
        ds = pydicom.dcmread(paths_pelvis[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[125:128, 60:69] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_pelvis[2], write_like_original=False)

        result_pelvis = self.engine.analyze_series(paths_pelvis)
        truncation_flags_pelvis = [f for f in result_pelvis.flags if "TRUNCATION_ERROR" in f.message]
        self.assertGreater(len(truncation_flags_pelvis), 0, "Pelvis scan should not ignore posterior table contact")

    def test_cavity_scout_gas_detection(self):
        # 1. Test moderate gas volume on pelvic scan (lower 50% only) -> Should be CONDITIONAL
        paths_pelvis_mod = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study", num_slices=10, pixel_spacing=[1.5, 1.5])
        for path in paths_pelvis_mod:
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Body contour (radius 32)
            body_mask = (x - 64)**2 + (y - 64)**2 <= 32**2
            pixels[body_mask] = 924
            # Moderate gas cavity (radius 22 -> ~34.2 cc across lower 5 slices)
            cavity_mask = (x - 64)**2 + (y - 64)**2 <= 22**2
            pixels[cavity_mask] = 24
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, write_like_original=False)

        result_mod = self.engine.analyze_series(paths_pelvis_mod)
        self.assertGreater(result_mod.metrics["gas_volume_cc"], 15.0)
        self.assertLessEqual(result_mod.metrics["gas_volume_cc"], 50.0)

        gas_flags_mod = [f for f in result_mod.flags if f.name == "CavityScout"]
        self.assertEqual(len(gas_flags_mod), 1)
        self.assertEqual(gas_flags_mod[0].status, "CONDITIONAL")
        self.assertNotIn("SEGMENTATION_LEAK", gas_flags_mod[0].message)

        # 2. Test excessive gas volume on pelvic scan (lower 50% only) -> Should be REJECT
        paths_pelvis_exc = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study", num_slices=10, pixel_spacing=[1.5, 1.5])
        for path in paths_pelvis_exc:
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Body contour (radius 48)
            body_mask = (x - 64)**2 + (y - 64)**2 <= 48**2
            pixels[body_mask] = 924
            # Excessive gas cavity (radius 32 -> ~68.4 cc across lower 5 slices)
            cavity_mask = (x - 64)**2 + (y - 64)**2 <= 32**2
            pixels[cavity_mask] = 24
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, write_like_original=False)

        result_exc = self.engine.analyze_series(paths_pelvis_exc)
        self.assertGreater(result_exc.metrics["gas_volume_cc"], 50.0)
        self.assertLessEqual(result_exc.metrics["gas_volume_cc"], 100.0)

        gas_flags_exc = [f for f in result_exc.flags if f.name == "CavityScout"]
        self.assertEqual(len(gas_flags_exc), 1)
        self.assertEqual(gas_flags_exc[0].status, "REJECT")
        self.assertNotIn("SEGMENTATION_LEAK", gas_flags_exc[0].message)

        # 3. Test massive gas volume (>100 cc) on pelvic scan -> Should trigger SEGMENTATION_LEAK
        paths_pelvis_leak = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study", num_slices=10, pixel_spacing=[1.5, 1.5])
        for path in paths_pelvis_leak:
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Body contour (radius 60)
            body_mask = (x - 64)**2 + (y - 64)**2 <= 60**2
            pixels[body_mask] = 924
            # Massive gas cavity (radius 46 -> ~149 cc across lower 5 slices)
            cavity_mask = (x - 64)**2 + (y - 64)**2 <= 46**2
            pixels[cavity_mask] = 24
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, write_like_original=False)

        result_leak = self.engine.analyze_series(paths_pelvis_leak)
        self.assertGreater(result_leak.metrics["gas_volume_cc"], 100.0)

        gas_flags_leak = [f for f in result_leak.flags if f.name == "CavityScout"]
        self.assertEqual(len(gas_flags_leak), 1)
        self.assertEqual(gas_flags_leak[0].status, "REJECT")
        self.assertIn("SEGMENTATION_LEAK", gas_flags_leak[0].message)

        # 4. Test thoracic scan bypass -> Gas volume should be 0.0 and no CavityScout flags
        paths_thorax = self.create_ct_series(protocol="Thorax Lung Scan", study_desc="Chest Thorax", num_slices=10, pixel_spacing=[1.5, 1.5])
        for path in paths_thorax:
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Body contour
            body_mask = (x - 64)**2 + (y - 64)**2 <= 24**2
            pixels[body_mask] = 924
            # Gas cavity
            cavity_mask = (x - 64)**2 + (y - 64)**2 <= 16**2
            pixels[cavity_mask] = 24
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, write_like_original=False)

        result_thorax = self.engine.analyze_series(paths_thorax)
        self.assertEqual(result_thorax.metrics["gas_volume_cc"], 0.0)
        gas_flags_thorax = [f for f in result_thorax.flags if f.name == "CavityScout"]
        self.assertEqual(len(gas_flags_thorax), 0)

    def test_cavity_scout_disconnected_gas(self):
        # Create a pelvic scan with 10 slices
        paths = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study", num_slices=10, pixel_spacing=[1.5, 1.5])

        # We will add gas in slices 1, 2 and slices 4, 5. Slice 3 will have NO gas.
        # This creates two disconnected gas bubbles separated by a slice with no gas.
        # Slices 1, 2, 4, 5 correspond to 0-indexed indices 0, 1, 3, 4. These are in the lower 50% (slices 0 to 4).
        for i, path in enumerate(paths):
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]

            # Body contour (radius 48)
            body_mask = (x - 64)**2 + (y - 64)**2 <= 48**2
            pixels[body_mask] = 924

            # Add gas in slices 0, 1 (first bubble) and slices 3, 4 (second bubble)
            if i in [0, 1, 3, 4]:
                # Gas cavity (radius 20 -> ~5.6 cc per slice, ~11.3 cc per bubble)
                cavity_mask = (x - 64)**2 + (y - 64)**2 <= 20**2
                pixels[cavity_mask] = 24

            ds.PixelData = pixels.tobytes()
            ds.save_as(path, write_like_original=False)

        result = self.engine.analyze_series(paths)

        # Verify total gas volume is combined (around 22.6 cc)
        self.assertGreater(result.metrics["gas_volume_cc"], 20.0)
        self.assertLess(result.metrics["gas_volume_cc"], 25.0)

        # Verify the largest connected component is around 11.3 cc
        self.assertGreater(result.metrics["largest_gas_volume_cc"], 10.0)
        self.assertLess(result.metrics["largest_gas_volume_cc"], 12.0)

        # Verify the gas_slices metric only has the slices of the largest bubble (slices 1 and 2, or slices 4 and 5)
        # Slices indices: [1, 2] or [4, 5]
        self.assertEqual(len(result.metrics["gas_slices"]), 2)
        self.assertTrue(result.metrics["gas_slices"] == [1, 2] or result.metrics["gas_slices"] == [4, 5])

        # Since largest gas volume is ~11.3 cc, which is <= 15.0 cc, there should be no CavityScout flag (PASS)
        gas_flags = [f for f in result.flags if f.name == "CavityScout"]
        self.assertEqual(len(gas_flags), 0, "No CavityScout flags should be triggered since neither individual bubble exceeds 15.0 cc")

        # Now, let's make one of the bubbles larger (e.g. radius 25 in the second bubble, which is ~17.6 cc)
        # This should trigger a CONDITIONAL flag, and report the slice range of that larger bubble [4, 5]
        for i, path in enumerate(paths):
            if i in [3, 4]:
                ds = pydicom.dcmread(path)
                pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
                y, x = np.ogrid[:128, :128]
                body_mask = (x - 64)**2 + (y - 64)**2 <= 48**2
                pixels[body_mask] = 924
                # Larger gas cavity (radius 25 -> ~8.8 cc per slice, ~17.6 cc per bubble)
                cavity_mask = (x - 64)**2 + (y - 64)**2 <= 25**2
                pixels[cavity_mask] = 24
                ds.PixelData = pixels.tobytes()
                ds.save_as(path, write_like_original=False)

        result_large = self.engine.analyze_series(paths)

        # Largest bubble should be around 17.6 cc
        self.assertGreater(result_large.metrics["largest_gas_volume_cc"], 16.0)
        self.assertLess(result_large.metrics["largest_gas_volume_cc"], 19.0)

        # Slices should be [4, 5]
        self.assertEqual(result_large.metrics["gas_slices"], [4, 5])

        # CavityScout flag should be CONDITIONAL (15 cc < volume <= 50 cc)
        gas_flags_large = [f for f in result_large.flags if f.name == "CavityScout"]
        self.assertEqual(len(gas_flags_large), 1)
        self.assertEqual(gas_flags_large[0].status, "CONDITIONAL")
        self.assertIn("Moderate gas volume", gas_flags_large[0].message)
        self.assertIn("Slices 4-5", gas_flags_large[0].message)

if __name__ == '__main__':
    unittest.main()
