@echo off
REM One-time setup for the Kalshi Engine. Needs Python 3.11+ from python.org (tick "Add python.exe to PATH").
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
python -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || (echo Install failed - check your internet connection. & pause & exit /b 1)
echo.
echo Opening Windows Firewall for your phone (click Yes on the prompt)...
call "%~dp0allow_phone.bat"
echo.
echo Setup complete. Optional: double-click setup_ai.bat to add the local AI forecaster (graphics card).
echo Then double-click run_engine.bat to start.
pause
