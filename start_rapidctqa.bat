@echo off
echo ========================================
echo       RapidCTQA Control Center
echo ========================================
cd /d "%~dp0"

if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

echo Starting RapidCTQA Backend...
echo Web Dashboard: http://localhost:8000
echo.

:: Automatically open the dashboard
start http://localhost:8000

python run.py
pause
