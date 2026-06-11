import unittest
from unittest.mock import patch, mock_open
import json
import os
from backend.dicom_sender import get_destinations

class TestDicomSender(unittest.TestCase):
    @patch("builtins.open", new_callable=mock_open, read_data='{"dicom_destinations": [{"ae_title": "TEST_AE", "is_active": true, "ip_address": "127.0.0.1", "port": 11112}]}')
    @patch("os.path.exists", return_value=True)
    def test_get_destinations_active(self, mock_exists, mock_file):
        dests = get_destinations()
        self.assertEqual(len(dests), 1)
        self.assertEqual(dests[0]["ae_title"], "TEST_AE")

    @patch("builtins.open", new_callable=mock_open, read_data='{"dicom_destinations": [{"ae_title": "TEST_AE", "is_active": false, "ip_address": "127.0.0.1", "port": 11112}]}')
    @patch("os.path.exists", return_value=True)
    def test_get_destinations_inactive(self, mock_exists, mock_file):
        dests = get_destinations()
        self.assertEqual(len(dests), 0)

if __name__ == "__main__":
    unittest.main()
