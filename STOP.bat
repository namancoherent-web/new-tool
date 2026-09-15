@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Market Universe Finder — stopping everything
echo ============================================
echo.
echo Closing terminal windows does not always kill the backend process or
echo any Chromium windows it opened -- this forcibly stops all of them by
echo looking at what each process is actually running, not just its name.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"

echo.
echo ============================================
echo  Done. This tool's processes have been stopped.
echo  Your regular Chrome browser (if open) was left alone.
echo ============================================
pause
