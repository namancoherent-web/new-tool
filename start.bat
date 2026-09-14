@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  Market Universe Finder — starting up
echo ============================================
echo.

if not exist ".env" (
    echo [ERROR] .env file not found.
    echo Copy .env.example to .env and add your DEEPSEEK_API_KEY first.
    echo See REQUIREMENTS.md for details.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo See REQUIREMENTS.md for install instructions.
    pause
    exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js is not installed or not on PATH.
    echo See REQUIREMENTS.md for install instructions.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [Setup] Creating Python virtual environment - this only happens once...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo [Setup] Checking Python packages are up to date...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install Python packages.
    pause
    exit /b 1
)

if not exist "web\node_modules" (
    echo [Setup] Installing web interface packages - this only happens once and may take a few minutes...
    pushd web
    call npm install
    popd
    if errorlevel 1 (
        echo [ERROR] Failed to install web packages.
        pause
        exit /b 1
    )
)

if not exist "web\.env.local" (
    echo NEXT_PUBLIC_API_BASE=http://localhost:8000> "web\.env.local"
)

echo.
echo [Start] Launching backend and web interface...
echo   Backend:  http://localhost:8000
echo   Web app:  http://localhost:3000
echo.
echo Two new windows will open - leave them running while you use the tool.
echo Close this window (or both new windows) to stop everything.
echo.

start "Market Universe Finder — backend" cmd /k ""%~dp0.venv\Scripts\python.exe" -m uvicorn api.main:app --host localhost --port 8000"
timeout /t 3 /nobreak >nul
start "Market Universe Finder — web" cmd /k "cd /d "%~dp0web" && npm run dev"

timeout /t 5 /nobreak >nul
start "" "http://localhost:3000"

echo Done. You can close this window now — the backend and web windows will keep running.
pause
