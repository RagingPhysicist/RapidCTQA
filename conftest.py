"""Test-session setup shared by every test under backend/.

Point storage and logs at throw-away directories *before* any test imports
the application, so running the suite can never read, write or clean up a
real DICOM share or the clinical QA logs, whatever webApp.local.yaml says.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="rapidctqa-tests-")
os.environ["RAPIDCTQA_STORAGE_DIR"] = os.path.join(_TMP, "storage")
os.environ["RAPIDCTQA_LOGS_DIR"] = os.path.join(_TMP, "logs")
os.environ["RAPIDCTQA_EXPORT_DIR"] = os.path.join(_TMP, "export")
os.environ["RAPIDCTQA_REPORTS_DIR"] = os.path.join(_TMP, "reports")
os.environ["RAPIDCTQA_CONFIG_LOCAL"] = os.path.join(_TMP, "no-local-config.yaml")
