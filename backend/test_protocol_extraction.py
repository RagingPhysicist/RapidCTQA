import unittest
from unittest.mock import MagicMock, patch
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
import numpy as np
import os
import shutil
from backend.engine import QAEngine

class TestProtocolExtraction(unittest.TestCase):
    def setUp(self):
        # Create a minimal config for QAEngine
        self.config_path = "test_ctqa.yaml"
        with open(self.config_path, "w") as f:
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
        self.test_dir = "test_data_protocol"
        os.makedirs(self.test_dir, exist_ok=True)

    def tearDown(self):
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def create_ct_dataset(self, sop_uid, protocol="TestProtocol"):
        ds = Dataset()
        ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.2' # CT
        ds.SOPInstanceUID = sop_uid
        ds.SeriesInstanceUID = '1.2.3'
        ds.PatientName = "TestPatient"
        ds.ProtocolName = protocol
        ds.Rows = 128
        ds.Columns = 128
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = 2.0
        ds.ImagePositionPatient = [0.0, 0.0, 0.0]
        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0
        ds.PixelData = np.zeros((128, 128), dtype=np.uint16).tobytes()

        file_meta = FileMetaDataset()
        file_meta.TransferSyntaxUID = '1.2.840.10008.1.2.1'
        file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta = file_meta
        return ds

    def create_rtss_dataset(self, sop_uid):
        ds = Dataset()
        ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.481.3' # RTSS
        ds.SOPInstanceUID = sop_uid
        ds.SeriesInstanceUID = '1.2.3.rtss'
        ds.PatientName = "TestPatient"
        # RTSS often doesn't have ProtocolName, or it's different

        file_meta = FileMetaDataset()
        file_meta.TransferSyntaxUID = '1.2.840.10008.1.2.1'
        file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta = file_meta
        return ds

    def test_protocol_extraction_with_rtss(self):
        # Create one RTSS file (named alphabetically first)
        rtss_path = os.path.join(self.test_dir, "000_rtss.dcm")
        rtss_ds = self.create_rtss_dataset("1.2.3.999")
        rtss_ds.save_as(rtss_path, write_like_original=False)

        # Create one CT file
        ct_path = os.path.join(self.test_dir, "001_ct.dcm")
        ct_ds = self.create_ct_dataset("1.2.3.1", protocol="RealProtocol")
        ct_ds.save_as(ct_path, write_like_original=False)

        # Analyze
        dicom_files = [rtss_path, ct_path]
        result = self.engine.analyze_series(dicom_files)

        # Verify protocol is from CT, not Unknown (which RTSS would give if it was first and not filtered)
        self.assertEqual(result.protocol, "RealProtocol")

    def test_protocol_extraction_fallback(self):
        # First CT has no ProtocolName (Unknown), second has it
        ct1_path = os.path.join(self.test_dir, "ct1.dcm")
        ct1_ds = self.create_ct_dataset("1.2.3.1", protocol="Unknown")
        ct1_ds.save_as(ct1_path, write_like_original=False)

        ct2_path = os.path.join(self.test_dir, "ct2.dcm")
        ct2_ds = self.create_ct_dataset("1.2.3.2", protocol="ValidProtocol")
        ct2_ds.ImagePositionPatient = [0.0, 0.0, 2.0] # Different Z
        ct2_ds.save_as(ct2_path, write_like_original=False)

        result = self.engine.analyze_series([ct1_path, ct2_path])
        self.assertEqual(result.protocol, "ValidProtocol")

if __name__ == '__main__':
    unittest.main()
