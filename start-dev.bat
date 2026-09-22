@echo off
rem Agent Office - development mode (FastAPI on 8000, Vite on 5173)
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Agent Office - Dev Launcher

set "VENV=%USERPROFILE%\.verdent\agentforge-venv"
if not exist "%VENV%\Scripts\python.exe" set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo [!] Python venv not found at %VENV%
    echo     python -m venv "%USERPROFILE%\.verdent\agentforge-venv" ^&^& "%USERPROFILE%\.verdent\agentforge-venv\Scripts\pip" install -r backend\requirements.txt
    pause
    exit /b 1
)

netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul 2>&1
if errorlevel 1 (
    start "Agent Office API" "%VENV%\Scripts\python.exe" "%~dp0backend\run_server.pyw"
) else (
    echo [*] FastAPI already running on http://127.0.0.1:8000
)

netstat -ano | findstr /r /c:":5173 .*LISTENING" >nul 2>&1
if errorlevel 1 (
    start "Agent Office Vite" cmd /k "pushd ""%~dp0frontend"" && npm install && npm run dev -- --host 127.0.0.1"
) else (
    echo [*] Vite already running on http://127.0.0.1:5173
)

start "" http://127.0.0.1:5173/
exit /b 0
