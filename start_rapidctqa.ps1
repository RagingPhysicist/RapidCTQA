Write-Host "Starting RapidCTQA Portal..." -ForegroundColor Cyan
Set-Location $PSScriptRoot

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    & ".\.venv\Scripts\Activate.ps1"
}

python run.py
Read-Host "Press Enter to exit"
