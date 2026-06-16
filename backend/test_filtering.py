import unittest
from unittest.mock import MagicMock
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from backend.listener import DicomListener

class TestDicomFiltering(unittest.TestCase):
    def setUp(self):
        self.listener = DicomListener(storage_dir="/tmp/dicom", callback=MagicMock())

    def create_dummy_dataset(self, modality='CT', image_type=['ORIGINAL', 'PRIMARY', 'AXIAL'], sop_class='1.2.840.10008.5.1.4.1.1.2'):
        ds = Dataset()
        ds.Modality = modality
        ds.ImageType = image_type
        ds.SOPClassUID = sop_class
        ds.SeriesInstanceUID = '1.2.3'
        ds.SOPInstanceUID = '1.2.3.4'
        ds.PixelData = b'\x00' * 100

        file_meta = FileMetaDataset()
        # Media Storage SOP Class UID is (0002,0002)
        file_meta.MediaStorageSOPClassUID = sop_class
        file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        file_meta.TransferSyntaxUID = '1.2.840.10008.1.2.1'
        ds.file_meta = file_meta
        return ds

    def test_filter_ct_axial(self):
        # This should be accepted
        ds = self.create_dummy_dataset()
        event = MagicMock()
        event.dataset = ds
        event.file_meta = ds.file_meta

        # Mock os.makedirs and ds.save_as
        with unittest.mock.patch('os.makedirs'), \
             unittest.mock.patch('pydicom.dataset.Dataset.save_as'):
            status = self.listener._handle_store(event)
            self.assertEqual(status, 0x0000)
            self.assertIn('1.2.3', self.listener.series_tracker)

    def test_filter_scout(self):
        # This should be rejected (ignored)
        ds = self.create_dummy_dataset(image_type=['ORIGINAL', 'PRIMARY', 'LOCALIZER'])
        event = MagicMock()
        event.dataset = ds
        event.file_meta = ds.file_meta

        self.listener.series_tracker = {}
        with unittest.mock.patch('os.makedirs'), \
             unittest.mock.patch('pydicom.dataset.Dataset.save_as'):
            status = self.listener._handle_store(event)
            self.assertEqual(status, 0x0000)
            # It should NOT be in the tracker if we filtered it
            self.assertNotIn('1.2.3', self.listener.series_tracker)

    def test_filter_dose_report(self):
        # This should be rejected (ignored) - usually Secondary Capture or SR
        ds = self.create_dummy_dataset(modality='OT', sop_class='1.2.840.10008.5.1.4.1.1.7')
        event = MagicMock()
        event.dataset = ds
        event.file_meta = ds.file_meta

        self.listener.series_tracker = {}
        with unittest.mock.patch('os.makedirs'), \
             unittest.mock.patch('pydicom.dataset.Dataset.save_as'):
            status = self.listener._handle_store(event)
            self.assertEqual(status, 0x0000)
            self.assertNotIn('1.2.3', self.listener.series_tracker)

if __name__ == '__main__':
    unittest.main()
