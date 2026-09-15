# Forcibly stops this tool's backend, web interface, and any Chromium
# windows it opened for discovery -- by looking at what each process is
# actually running (command line / executable path), not just its process
# name, so a user's regular Chrome browser or unrelated Python/Node
# programs are never touched. Closing the visible terminal windows does
# not reliably kill these, since start.bat launches them as separate
# detached processes.

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn*'
} | ForEach-Object {
    Write-Host "  Stopping backend (PID $($_.ProcessId))"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'node.exe' -and $_.CommandLine -like '*next*'
} | ForEach-Object {
    Write-Host "  Stopping web interface (PID $($_.ProcessId))"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'chrome.exe' -and $_.ExecutablePath -like '*Chromium*'
} | ForEach-Object {
    Write-Host "  Stopping a Chromium window (PID $($_.ProcessId))"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'chromedriver.exe'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
