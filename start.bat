@echo off
cd /d "%~dp0"
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate
pip install -r requirements.txt --quiet
echo.
echo Starting Lumi Design AI Engine on http://localhost:8000
echo Press Ctrl+C to stop.
echo.
uvicorn main:app --port 8000
