import uvicorn
import os
import sys

# Ensure backend package is importable
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    print("Starting RapidCTQA Backend...")
    print("DICOM Listener: 0.0.0.0:11112")
    print("Web Dashboard: http://localhost:8000")
    
    from backend.main import app
    uvicorn.run(app, host="0.0.0.0", port=8000)
