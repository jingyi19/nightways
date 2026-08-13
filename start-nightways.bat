@echo off
setlocal

set "NIGHTWAYS_PYTHON=F:\codex-temp\nightways-fastapi-test\Scripts\python.exe"

if not exist "%NIGHTWAYS_PYTHON%" (
    echo Nightways could not find its isolated Python environment.
    echo Expected: %NIGHTWAYS_PYTHON%
    echo Install backend\requirements.txt into that environment first.
    pause
    exit /b 1
)

cd /d "%~dp0"

echo Starting Nightways...
echo Open http://127.0.0.1:8000/ in your browser.
echo Press Ctrl+C in this window to stop Nightways.
echo.

"%NIGHTWAYS_PYTHON%" -B -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
