@echo off
cd /d "%~dp0"
echo Stopping any previous NIFTY Options Engine process (if one was left running)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_engine.ps1"
echo Installing/checking dependencies...
python -m pip install -r requirements.txt
echo.
echo Starting NIFTY Options Engine dashboard (fresh process)...
python app.py
pause
