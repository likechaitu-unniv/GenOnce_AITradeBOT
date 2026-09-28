@echo off
cd /d "%~dp0"
echo Stopping NIFTY Options Engine (looking for any running app.py process)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_engine.ps1"
echo.
pause
