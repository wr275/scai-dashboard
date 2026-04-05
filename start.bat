@echo off
echo ============================================================
echo   SCAI Executive Dashboard — Starting Server
echo ============================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Install dependencies if needed
echo Checking dependencies...
pip install -r requirements.txt -q

echo.
echo Starting server at http://localhost:8000
echo Press Ctrl+C to stop.
echo.

:: Open browser after 2 seconds
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"

:: Start server
python server.py

pause
