@echo off
REM Starts the Kalshi Engine. Leave this window open (minimise it). Your phone connects to the address it prints.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m kbengine %*
pause
