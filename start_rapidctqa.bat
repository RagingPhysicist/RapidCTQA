@echo off
echo ========================================
echo       RapidCTQA Control Center
echo ========================================
cd /d "%~dp0"

if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

echo [1] Start Web Dashboard (FastAPI + DICOM Listener)
echo [2] Start Clinical Cockpit (Desktop App + Viewer)
echo.
set /p choice="Select System: "

if "%choice%"=="1" (
    echo Starting Backend...
    python run.py
) else if "%choice%"=="2" (
    echo Starting Clinical Cockpit...
    python cockpit.py
) else (
    echo Invalid choice.
)
pause
