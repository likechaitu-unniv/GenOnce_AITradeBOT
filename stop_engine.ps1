# Finds and force-stops any running "python ... app.py" process for this project.
# Used by stop.bat (manual stop) and run.bat (auto-cleanup before every start),
# so a leftover process from a terminal window that was closed with the X button
# (which does not reliably kill the child python process on Windows) can never
# silently keep serving stale code alongside a newer process.
$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'app\.py' }
if ($procs) {
    foreach ($p in $procs) {
        Write-Host ("Stopping PID " + $p.ProcessId + " (started " + $p.CreationDate + ")")
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    Write-Host "Done - all app.py processes stopped."
} else {
    Write-Host "Nothing was running - no app.py process found."
}
