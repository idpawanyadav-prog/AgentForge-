@echo off
rem Agent Office - start backend + current frontend build (served by FastAPI on port 8000)
rem Safe to run twice (won't start a second copy). The server runs in its
rem own VISIBLE console window with live logs - closing that window stops
rem the server (or use stop.bat).
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Agent Office - Launcher

rem venv lives outside the workspace (excluded from Verdent source packaging)
set "VENV=%USERPROFILE%\.verdent\agentforge-venv"
if not exist "%VENV%\Scripts\python.exe" set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo [!] Python venv not found at %VENV% - create it first:
    echo     python -m venv "%USERPROFILE%\.verdent\agentforge-venv" ^&^& "%USERPROFILE%\.verdent\agentforge-venv\Scripts\pip" install -r backend\requirements.txt
    pause
    exit /b 1
)

rem Already running? Just open the UI instead of spawning a duplicate.
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Agent Office is already running on http://127.0.0.1:8000
    start "" http://127.0.0.1:8000/
    ping -n 4 127.0.0.1 >nul
    exit /b 0
)

rem Fast frontend prep: install deps only when node_modules is missing (never
rem the slow, destructive `npm ci`), and rebuild only when a source file is
rem newer than the built bundle. Set AF_FORCE_BUILD=1 to force a rebuild.
echo [*] Preparing frontend...
if not exist "frontend\node_modules\" (
    echo     installing frontend dependencies (first run only)...
    pushd frontend
    call npm install
    if errorlevel 1 (popd & echo [!] frontend dependencies failed & pause & exit /b 1)
    popd
)

set "NEEDS_BUILD=1"
if not "%AF_FORCE_BUILD%"=="1" (
    set "NEEDS_BUILD=0"
    if not exist "backend\static\index.html" set "NEEDS_BUILD=1"
)
if "%NEEDS_BUILD%"=="0" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\frontend_stale.ps1"
    if errorlevel 1 set "NEEDS_BUILD=1"
)

if "%NEEDS_BUILD%"=="0" (
    echo     build is up to date - skipping
) else (
    echo     building frontend into backend\static...
    pushd frontend
    call npm run build
    if errorlevel 1 (popd & echo [!] frontend build failed & pause & exit /b 1)
    popd
    if exist backend\static rmdir /s /q backend\static
    xcopy /e /i /y frontend\dist backend\static >nul || (echo [!] copying frontend build failed & pause & exit /b 1)
)

echo Starting Agent Office on http://127.0.0.1:8000 ...
echo The server opens in its own console window (live logs). Closing that
echo window stops the server; you can also stop it with stop.bat.
rem Visible server window: python.exe keeps a console open with live logs.
start "Agent Office Server" "%VENV%\Scripts\python.exe" "%~dp0backend\run_server.pyw"

rem Wait until the server answers (max ~30s). ping is used as a sleep that
rem also works when stdin is redirected.
set /a tries=0
:waitloop
ping -n 3 127.0.0.1 >nul
curl -s -o nul --max-time 3 http://127.0.0.1:8000/
if not errorlevel 1 goto up
set /a tries+=1
if !tries! lss 15 goto waitloop
echo [!] Server did not become healthy within ~30s. Check the server window
echo     for errors, or see server.err.log in this folder.
pause
exit /b 1

:up
echo Agent Office is up: http://127.0.0.1:8000
start "" http://127.0.0.1:8000/
ping -n 4 127.0.0.1 >nul
exit /b 0
