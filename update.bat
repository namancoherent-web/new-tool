@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Market Universe Finder — updating
echo ============================================
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Git is not installed or not on PATH.
    echo See REQUIREMENTS.md for install instructions.
    pause
    exit /b 1
)

if not exist ".git" (
    echo [ERROR] This folder is not set up as a git checkout, so it can't
    echo be auto-updated. Ask whoever set this up to re-share the folder.
    pause
    exit /b 1
)

echo [Update] Fetching the latest version from GitHub...
git fetch origin main
if errorlevel 1 (
    echo [ERROR] Could not reach GitHub. Check your internet connection.
    pause
    exit /b 1
)

echo [Update] Applying the latest version (this overwrites any local edits
echo          to the tool's own files — your .env file is not touched)...
git reset --hard origin/main
if errorlevel 1 (
    echo [ERROR] Update failed to apply.
    pause
    exit /b 1
)

git clean -fd -e .env -e .env.local -e web/.env.local >nul 2>nul

echo.
echo [Update] Refreshing installed packages to match the new version...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)
if exist "web\node_modules" (
    pushd web
    call npm install
    popd
)

echo.
echo ============================================
echo  Update complete. Run start.bat to launch.
echo ============================================
pause
