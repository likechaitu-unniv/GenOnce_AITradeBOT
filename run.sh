#!/bin/bash
cd "$(dirname "$0")"
echo "Stopping any previous NIFTY Options Engine process (if one was left running)..."
pkill -f "python3? .*app\.py" 2>/dev/null
sleep 1
echo "Installing/checking dependencies..."
python3 -m pip install -r requirements.txt
echo
echo "Starting NIFTY Options Engine dashboard (fresh process)..."
python3 app.py
