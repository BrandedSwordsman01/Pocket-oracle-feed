@echo off
REM Makes the engine start automatically when you log in to Windows.
cd /d "%~dp0"
schtasks /Create /F /SC ONLOGON /TN "KalshiEngine" /TR "\"%~dp0run_engine.bat\"" /RL LIMITED
echo The engine will now start each time you log in. (Remove with: schtasks /Delete /TN KalshiEngine /F)
pause
