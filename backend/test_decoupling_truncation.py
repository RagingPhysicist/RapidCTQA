import unittest
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
import numpy as np
import os
import shutil
from backend.engine import QAEngine
from backend.utils import segment_patient_and_accessories, detect_couch_plane

class TestDecouplingTruncation(unittest.TestCase):
    def setUp(self):
        self.config_path = "test_ctqa_decoupling.yaml"
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("""
thresholds:
  geometry:
    edge_buffer_px: 3
    max_slice_spacing_variation_mm: 1.0
    max_gantry_tilt_deg: 1.0
  implants:
    metal_threshold_hu: 2000
    max_volume_cc: 0.05
  alignment:
    hu_floor: -300
    angular_step_deg: 0.1
    max_allowable_tilt_deg: 1.5
""")
        self.engine = QAEngine(self.config_path)
        self.test_dir = "test_data_decoupling"
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def create_ct_series(self, protocol="Pelvis Prostate", study_desc="Pelvis Study", body_part="PELVIS", num_slices=5, pixel_spacing=[1.0, 1.0]):
        paths = []
        for i in range(num_slices):
            ds = Dataset()
            ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.2'
            ds.SOPInstanceUID = f"1.2.840.10008.{i}"
            ds.SeriesInstanceUID = "1.2.840.10008.series"
            ds.PatientName = "DecouplingTestPatient"
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

            # Default to ambient air (-1000 HU -> stored as 24)
            pixels = np.ones((128, 128), dtype=np.uint16) * 24
            ds.PixelData = pixels.tobytes()

            file_meta = FileMetaDataset()
            file_meta.TransferSyntaxUID = '1.2.840.10008.1.2.1'
            file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
            file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
            ds.file_meta = file_meta

            path = os.path.join(self.test_dir, f"slice_{i}.dcm")
            ds.save_as(path, enforce_file_format=True)
            paths.append(path)
        return paths

    def test_scan_with_clipped_couch_top_returns_pass_with_warning(self):
        """
        Deliverable 3 Assertion:
        Verify that a scan with a clipped couch top returns PASS_WITH_WARNING
        instead of a hard failure (FAIL_CRITICAL / REJECT).
        """
        paths = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Pelvis Study", num_slices=5)

        for i, path in enumerate(paths):
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]

            # 1. Patient anatomy centered safely in FOV (radius 22, center at (55, 64))
            patient_body = (x - 64)**2 + (y - 55)**2 <= 22**2
            pixels[patient_body] = 924  # -100 HU (normal tissue)

            # 2. Treatment table / carbon-fiber couch in lower quadrant clipped at bottom FOV edge
            # Couch top surface at y=85, extending down through rows 125-127 (touching outer 3 px)
            pixels[85:128, 15:113] = 924  # High attenuation couch structure touching bottom edge

            ds.PixelData = pixels.tobytes()
            ds.save_as(path, enforce_file_format=True)

        result = self.engine.analyze_series(paths)

        # 1. Verify Couch is classified in accessory_table_mask and NOT patient_body_mask
        self.assertTrue(result.metrics["accessory_truncation_detected"], "Clipped couch must be flagged as accessory truncation")
        self.assertFalse(result.metrics["truncation_error"], "Patient body must NOT be flagged as truncated")

        # 2. Verify GeometryGuardian flags PASS_WITH_WARNING
        gg_flags = [f for f in result.flags if f.name == "GeometryGuardian" and f.status != "SKIPPED"]
        self.assertEqual(len(gg_flags), 1)
        self.assertEqual(gg_flags[0].status, "PASS_WITH_WARNING", "Clipped couch top must return PASS_WITH_WARNING")
        self.assertIn("Accessory / Positioning Device Truncated at FOV Edge", gg_flags[0].message)

        # 3. Verify overall series status is PASS_WITH_WARNING (not FAIL_CRITICAL or REJECT)
        self.assertEqual(result.status, "PASS_WITH_WARNING", "Overall status must be PASS_WITH_WARNING instead of hard failure")

    def test_critical_patient_body_truncation_fails_critically(self):
        """
        Condition A (Critical Failure):
        Patient body intersects outermost border ring in anterior/posterior sector.
        Action: Output FAIL_CRITICAL ("Patient Body Truncation Detected").
        """
        paths = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Pelvis Study", num_slices=5)

        for i, path in enumerate(paths):
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]

            # Anterior patient truncation touching top edge (row 0 to 2)
            patient_body = (x - 64)**2 + (y - 20)**2 <= 22**2
            pixels[patient_body] = 924
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, enforce_file_format=True)

        result = self.engine.analyze_series(paths)

        self.assertTrue(result.metrics["truncation_error"])
        self.assertEqual(result.status, "FAIL_CRITICAL")

        gg_flags = [f for f in result.flags if f.name == "GeometryGuardian" and f.status == "FAIL_CRITICAL"]
        self.assertEqual(len(gg_flags), 1)
        self.assertIn("TRUNCATION_ERROR: Patient Body Truncation Detected", gg_flags[0].message)

    def test_flared_wingboard_elbow_clipping_suppression(self):
        """
        Task 3 Rule Matrix:
        Flared wingboard elbow clipping in Thorax/Breast:
        - depth < 15 mm -> PASS_WITH_WARNING
        - depth >= 15 mm -> FAIL_CRITICAL
        """
        # Case A: Depth = 10 mm (< 15 mm) on Thorax scan -> PASS_WITH_WARNING
        paths_tol = self.create_ct_series(protocol="Thorax Lung Scan", study_desc="Chest Thorax", num_slices=5)
        for i, path in enumerate(paths_tol):
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Patient core
            pixels[(x - 64)**2 + (y - 64)**2 <= 20**2] = 924
            # Flared lateral arm/elbow touching left FOV edge with 10 px depth (cols 0 to 9)
            if i == 2:
                pixels[58:68, 0:10] = 924
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, enforce_file_format=True)

        result_tol = self.engine.analyze_series(paths_tol)
        self.assertFalse(result_tol.metrics["truncation_error"])
        self.assertIn(3, result_tol.metrics["tolerated_truncated_slices"])
        gg_flags_tol = [f for f in result_tol.flags if f.name == "GeometryGuardian" and f.status == "PASS_WITH_WARNING"]
        self.assertEqual(len(gg_flags_tol), 1)
        self.assertIn("Flared wingboard elbow clipping within clinical tolerance (<15mm)", gg_flags_tol[0].message)

        # Case B: Depth = 20 mm (>= 15 mm) on Thorax scan -> FAIL_CRITICAL
        paths_crit = self.create_ct_series(protocol="Thorax Lung Scan", study_desc="Chest Thorax", num_slices=5)
        for i, path in enumerate(paths_crit):
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            pixels[(x - 64)**2 + (y - 64)**2 <= 20**2] = 924
            if i == 2:
                pixels[58:68, 0:20] = 924  # 20 px depth = 20 mm
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, enforce_file_format=True)

        result_crit = self.engine.analyze_series(paths_crit)
        self.assertTrue(result_crit.metrics["truncation_error"])
        self.assertEqual(result_crit.status, "FAIL_CRITICAL")

    def test_empty_air_slices_above_and_below_skipped(self):
        """
        Task 3 Rule Matrix:
        Empty air slices above head / below feet -> SKIPPED (EMPTY_SLICE)
        """
        paths = self.create_ct_series(protocol="H&N Brain", study_desc="Head Neck", num_slices=5)
        # Slices 0 and 4 are ambient air (-1000 HU)
        # Slices 1, 2, 3 have patient head
        for i in [1, 2, 3]:
            ds = pydicom.dcmread(paths[i])
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            pixels[(x - 64)**2 + (y - 64)**2 <= 20**2] = 924
            ds.PixelData = pixels.tobytes()
            ds.save_as(paths[i], enforce_file_format=True)

        result = self.engine.analyze_series(paths)
        self.assertIn(1, result.metrics["empty_slices"])
        self.assertIn(5, result.metrics["empty_slices"])
        self.assertFalse(result.metrics["truncation_error"])

        empty_flags = [f for f in result.flags if f.name == "GeometryGuardian" and f.status == "SKIPPED"]
        self.assertEqual(len(empty_flags), 1)
        self.assertIn("EMPTY_SLICE: Over-range air slices bypassed", empty_flags[0].message)
        self.assertIn("Slices 1, 5", empty_flags[0].message)

    def test_pelvic_rectal_gas_volume_under_15cc_passes(self):
        """
        Task 3 Rule Matrix:
        Pelvic Rectal Gas Volume < 15 cm3 -> PASS (Normal physiological variance)
        """
        paths = self.create_ct_series(protocol="Pelvis Prostate", study_desc="Prostate Study", num_slices=10, pixel_spacing=[1.5, 1.5])
        for path in paths:
            ds = pydicom.dcmread(path)
            pixels = np.frombuffer(ds.PixelData, dtype=np.uint16).copy().reshape((128, 128))
            y, x = np.ogrid[:128, :128]
            # Patient body
            pixels[(x - 64)**2 + (y - 64)**2 <= 30**2] = 924
            # Small gas pocket (radius 10 -> ~7 cc across lower 5 slices)
            pixels[(x - 64)**2 + (y - 64)**2 <= 10**2] = 24  # Air cavity
            ds.PixelData = pixels.tobytes()
            ds.save_as(path, enforce_file_format=True)

        result = self.engine.analyze_series(paths)
        gas_cc = result.metrics["gas_volume_cc"]
        self.assertGreater(gas_cc, 0.0)
        self.assertLessEqual(gas_cc, 15.0)

        cavity_flags = [f for f in result.flags if f.name == "CavityScout"]
        self.assertEqual(len(cavity_flags), 1)
        self.assertEqual(cavity_flags[0].status, "PASS", "Gas < 15 cc must return PASS")
        self.assertIn("within physiological limits", cavity_flags[0].message)

    def test_three_stage_anatomical_decoupling_masks(self):
        """
        Task 1 Deliverable:
        Verify that segment_patient_and_accessories generates distinct,
        mutually exclusive patient_body_mask and accessory_table_mask.
        """
        vol = np.ones((5, 128, 128), dtype=float) * -1000.0
        y, x = np.ogrid[:128, :128]
        # Patient torso
        vol[:, ((x - 64)**2 / 25**2 + (y - 55)**2 / 20**2) <= 1.0] = -100.0
        # Treatment couch in lower quadrant
        vol[:, 85:95, 15:113] = 50.0
        # Vac-bag bridge connecting posterior skin to couch
        vol[:, 75:86, 60:68] = -250.0
        # Wingboard handle lateral contact
        vol[:, 52:62, 95:127] = -200.0

        p_mask, a_mask = segment_patient_and_accessories(vol, pixel_spacing=(1.0, 1.0))

        # Assert masks are mutually exclusive
        overlap = np.any(p_mask & a_mask)
        self.assertFalse(overlap, "patient_body_mask and accessory_table_mask must be strictly mutually exclusive")

        # Assert patient body is detected and does not include couch
        self.assertGreater(p_mask.sum(), 0)
        self.assertFalse(np.any(p_mask[:, 85:95, 15:113]), "patient_body_mask must NOT contain treatment couch")

        # Assert couch and wingboard are isolated in accessory_table_mask
        self.assertTrue(np.all(a_mask[:, 88:94, 20:100]), "accessory_table_mask must capture the couch")
        self.assertTrue(np.all(a_mask[:, 54:60, 100:125]), "accessory_table_mask must capture the wingboard")

if __name__ == '__main__':
    unittest.main()
