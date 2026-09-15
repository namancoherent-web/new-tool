@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Market Universe Finder — one-time setup
echo ============================================
echo.
echo This will install anything missing (Git, Python, Node.js, Chromium)
echo and then start the tool. This window may take several minutes the
echo first time — that's normal. Don't close it.
echo.

where winget >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Windows Package Manager ^(winget^) was not found.
    echo This usually means Windows needs an update, or "App Installer"
    echo needs to be installed from the Microsoft Store first.
    echo Search "App Installer" in the Microsoft Store, install it, then
    echo run this file again.
    pause
    exit /b 1
)

echo [1/4] Checking Git...
where git >nul 2>nul
if errorlevel 1 (
    echo        Not found — installing Git ^(this can take a minute^)...
    winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
    call :refresh_path
) else (
    echo        Already installed.
)

echo [2/4] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
    echo        Not found — installing Python ^(this can take a minute^)...
    winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
    call :refresh_path
) else (
    echo        Already installed.
)

echo [3/4] Checking Node.js...
where node >nul 2>nul
if errorlevel 1 (
    echo        Not found — installing Node.js ^(this can take a minute^)...
    winget install --id OpenJS.NodeJS.LTS -e --silent --accept-source-agreements --accept-package-agreements
    call :refresh_path
) else (
    echo        Already installed.
)

echo [4/4] Checking Chromium...
if not exist "%LOCALAPPDATA%\Chromium\Application\chrome.exe" (
    echo        Not found — installing Chromium ^(this can take a few minutes^)...
    winget install --id Hibbiki.Chromium -e --silent --accept-source-agreements --accept-package-agreements
) else (
    echo        Already installed.
)

echo.
echo [Setup] Core programs ready. Refreshing this window's PATH...
call :refresh_path

if not exist ".env" (
    if exist ".env.example" (
        echo.
        echo [Setup] Creating your .env file from the template...
        copy /y ".env.example" ".env" >nul
        echo.
        echo ============================================
        echo  ACTION NEEDED
        echo ============================================
        echo Open the new .env file in this folder with Notepad, find the
        echo line that starts with DEEPSEEK_API_KEY=, and paste your API
        echo key right after the = sign. Save the file, then run this
        echo SETUP.bat again ^(or start.bat next time^).
        echo.
        notepad ".env"
        pause
        exit /b 0
    )
)

echo.
echo [Setup] Handing off to start.bat...
echo.
call start.bat
exit /b 0

:refresh_path
for /f "usebackq tokens=2,*" %%A in (`reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul`) do set "SYS_PATH=%%B"
for /f "usebackq tokens=2,*" %%A in (`reg query "HKCU\Environment" /v Path 2^>nul`) do set "USER_PATH=%%B"
set "PATH=%SYS_PATH%;%USER_PATH%"
exit /b 0
