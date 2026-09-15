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
    echo [Setup] This folder came from a ZIP file, not git yet — connecting
    echo         it to GitHub now so future updates can pull automatically.
    echo         This only needs to happen once.
    echo.

    if exist ".env" (
        copy /y ".env" "%TEMP%\market_universe_finder_env_backup" >nul
    )
    if exist "web\.env.local" (
        copy /y "web\.env.local" "%TEMP%\market_universe_finder_weblocal_backup" >nul
    )

    git init >nul
    git remote add origin https://github.com/namancoherent-web/new-tool.git
    git fetch origin main
    if errorlevel 1 (
        echo [ERROR] Could not reach GitHub. Check your internet connection.
        pause
        exit /b 1
    )
    git reset --hard origin/main

    if exist "%TEMP%\market_universe_finder_env_backup" (
        copy /y "%TEMP%\market_universe_finder_env_backup" ".env" >nul
        del "%TEMP%\market_universe_finder_env_backup" >nul
    )
    if exist "%TEMP%\market_universe_finder_weblocal_backup" (
        copy /y "%TEMP%\market_universe_finder_weblocal_backup" "web\.env.local" >nul
        del "%TEMP%\market_universe_finder_weblocal_backup" >nul
    )

    echo [Setup] Connected. Continuing with the update...
    echo.
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

git clean -fd -e .env -e .env.local -e web/.env.local -e chrome_profile -e outputs -e logs -e cache >nul 2>nul

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
