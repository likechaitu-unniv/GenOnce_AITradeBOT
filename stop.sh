#!/bin/bash
cd "$(dirname "$0")"
echo "Stopping NIFTY Options Engine (looking for any running app.py process)..."
if pkill -f "python3? .*app\.py"; then
  echo "Done - all app.py processes stopped."
else
  echo "Nothing was running - no app.py process found."
fi
