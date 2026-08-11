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
        # With our new robust body-contour-based segmentation, the treatment table/couch is
        # completely excluded from the patient's body mask (interior_mask), so both Head & Neck
        # scans and Pelvis scans should naturally accept posterior table contact and not flag any truncation error.

        # 1. H&N Scan -> Should accept posterior edge table contact
        paths_hn = self.create_ct_series(protocol="Head scan", study_desc="Neck C-Spine")
        ds = pydicom.dcmread(paths_hn[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[125:128, 60:69] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_hn[2], write_like_original=False)

        result_hn = self.engine.analyze_series(paths_hn)
        truncation_flags_hn = [f for f in result_hn.flags if "TRUNCATION_ERROR" in f.message]
        self.assertEqual(len(truncation_flags_hn), 0, "H&N scan should ignore posterior table contact due to body segmentation")

        # 2. Pelvis Scan -> Should also accept posterior edge table contact under robust body segmentation
        paths_pelvis = self.create_ct_series(protocol="Pelvis scan", study_desc="Pelvis Study")
        ds = pydicom.dcmread(paths_pelvis[2])
        pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels[125:128, 60:69] = 924
        ds.PixelData = pixels.tobytes()
        ds.save_as(paths_pelvis[2], write_like_original=False)

        result_pelvis = self.engine.analyze_series(paths_pelvis)
        truncation_flags_pelvis = [f for f in result_pelvis.flags if "TRUNCATION_ERROR" in f.message]
        self.assertEqual(len(truncation_flags_pelvis), 0, "Pelvis scan should ignore posterior table contact due to body segmentation")

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

    def test_empty_slice_rejection(self):
        # Create a series where slice 0 is completely empty, slice 1 has some noise (< 500 contiguous pixels, e.g. 5x5 pixels),
        # and slices 2, 3, 4 have a large body/patient core (radius 20 pixels -> >1200 pixels).
        paths = self.create_ct_series(protocol="H&N C-Spine", study_desc="Brain Study", num_slices=5)

        # Modify slice 1 to have small noise / non-empty but extremely small tissue area
        ds_1 = pydicom.dcmread(paths[1])
        pixels_1 = np.frombuffer(ds_1.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels_1[60:65, 60:65] = 924 # 25 pixels -> under min_voxels threshold of ~31 (scaled from 500 for 128x128)
        ds_1.PixelData = pixels_1.tobytes()
        ds_1.save_as(paths[1], write_like_original=False)

        # Modify slice 2, 3, 4 to have valid patient tissue
        for idx in [2, 3, 4]:
            ds = pydicom.dcmread(paths[idx])
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            body_mask = (x - 64)**2 + (y - 64)**2 <= 20**2 # ~1250 pixels
            pixels[body_mask] = 924
            ds.PixelData = pixels.tobytes()
            ds.save_as(paths[idx], write_like_original=False)

        result = self.engine.analyze_series(paths)
        self.assertIn("empty_slices", result.metrics)
        # Slices 1 and 2 (0-indexed indices 0 and 1) should be detected as empty
        self.assertEqual(result.metrics["empty_slices"], [1, 2])

    def test_empty_slice_rejection_with_couch_accessories(self):
        # Slice 0 has couch/immobilization accessories but no patient.
        # The couch accessory is low-density (e.g. -400 HU -> stored as 624).
        # Slices 1, 2, 3, 4 have valid patient tissue.
        paths = self.create_ct_series(protocol="H&N C-Spine", study_desc="Brain Study", num_slices=5)

        # Modify slice 0 to have a couch accessory (large component but HU < -200 HU, e.g., -400 HU)
        ds_0 = pydicom.dcmread(paths[0])
        pixels_0 = np.frombuffer(ds_0.PixelData, dtype=np.uint16).copy().reshape((128, 128))
        pixels_0[50:80, 20:100] = 624 # -400 HU (tissue_threshold_hu is -300 HU, so this is > -500 but < -200)
        ds_0.PixelData = pixels_0.tobytes()
        ds_0.save_as(paths[0], write_like_original=False)

        # Modify slice 1, 2, 3, 4 to have valid patient tissue
        for idx in [1, 2, 3, 4]:
            ds = pydicom.dcmread(paths[idx])
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            body_mask = (x - 64)**2 + (y - 64)**2 <= 20**2
            pixels[body_mask] = 924 # -100 HU (> -200 HU)
            ds.PixelData = pixels.tobytes()
            ds.save_as(paths[idx], write_like_original=False)

        result = self.engine.analyze_series(paths)
        self.assertIn("empty_slices", result.metrics)
        # Slice 1 (0-indexed index 0) should be detected as empty despite the couch accessory
        self.assertIn(1, result.metrics["empty_slices"])

    def test_dual_zone_accessory_truncation(self):
        # Create a series with a valid patient body in the center (radius 25 pixels)
        # And place a high density accessory (> -300 HU, e.g., -100 HU / 924 stored) in the outermost 3 pixels on slice 2.
        # But, do NOT touch the absolute patient contour to the edge.
        paths = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study", num_slices=5)

        for idx in range(5):
            ds = pydicom.dcmread(paths[idx])
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Center body (radius 25)
            body_mask = (x - 64)**2 + (y - 64)**2 <= 25**2
            pixels[body_mask] = 924

            # On slice 2, add high-density accessory touching the border (row 0 to 2, columns 40 to 60)
            if idx == 2:
                pixels[0:3, 40:61] = 924

            ds.PixelData = pixels.tobytes()
            ds.save_as(paths[idx], write_like_original=False)

        result = self.engine.analyze_series(paths)
        # Verify accessory truncation is detected on slice 3 (1-based)
        self.assertTrue(result.metrics["accessory_truncation_detected"])
        self.assertEqual(result.metrics["accessory_truncated_slices"], [3])
        # Verify critical truncation error is FALSE
        self.assertFalse(result.metrics["truncation_error"])

        # Verify flagged warning status is CONDITIONAL
        gg_flags = [f for f in result.flags if f.name == "GeometryGuardian"]
        self.assertEqual(len(gg_flags), 1)
        self.assertEqual(gg_flags[0].status, "CONDITIONAL")
        self.assertIn("Accessory/Table Truncation Detected", gg_flags[0].message)
        self.assertIn("Slice 3", gg_flags[0].message)

    def test_head_scan_non_circular_fov_corners_bypass(self):
        # Simulate a head scan with a rectangular / non-circular FOV (with cut-off corners).
        # We simulate this by placing high intensity values (e.g. tissue density, 924 stored / -100 HU)
        # at the extreme corners of the image matrix, touching the border, which under the old
        # threshold-based detector would trigger TRUNCATION_ERROR.
        # But, the actual patient body is located in the center (e.g. radius 20, well away from the borders).
        paths_head = self.create_ct_series(protocol="Head scan", study_desc="Brain Study")
        for path in paths_head:
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))

            # Place centered head tissue (radius 20)
            y, x = np.ogrid[:128, :128]
            head_mask = (x - 64)**2 + (y - 64)**2 <= 20**2
            pixels[head_mask] = 924 # -100 HU (normal tissue)

            # Place corner artifacts/cut-off corner boundaries touching the outermost edges
            # Top-left corner (0,0) to (5,5)
            pixels[0:6, 0:6] = 924
            # Top-right corner (0,122) to (5,127)
            pixels[0:6, 122:128] = 924
            # Bottom-left corner (122,0) to (127,5)
            pixels[122:128, 0:6] = 924
            # Bottom-right corner (122,122) to (127,127)
            pixels[122:128, 122:128] = 924

            ds.PixelData = pixels.tobytes()
            ds.save_as(path, write_like_original=False)

        result_head = self.engine.analyze_series(paths_head)

        # Verify that the body contour (interior_mask) isolates only the centered head component
        # and excludes the extreme corners (which are isolated or filtered out by vertical opening/dilation/erosion).
        # Thus, no truncation should be flagged.
        truncation_flags = [f for f in result_head.flags if "TRUNCATION_ERROR" in f.message]
        self.assertEqual(len(truncation_flags), 0, "Non-circular FOV corners should not trigger truncation error")
        self.assertFalse(result_head.metrics["truncation_error"])

if __name__ == '__main__':
    unittest.main()
