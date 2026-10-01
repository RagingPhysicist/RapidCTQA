import uvicorn
import os
import sys

# Ensure backend package is importable
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    from backend import settings
    from backend.main import app

    print("Starting RapidCTQA Backend...")
    print(f"DICOM Listener: {settings.DICOM_HOST}:{settings.DICOM_PORT} (AET {settings.DICOM_AET})")
    print(f"Web Dashboard: http://{settings.API_HOST}:{settings.API_PORT}")
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT)
