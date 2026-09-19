@echo off
rem Agent Office - start backend + frontend (served by FastAPI on port 8000)
rem Resilient: safe to run twice (won't start a second copy), and the server
rem runs windowless/detached - closing this window does NOT stop it.
rem Logs: server.log / server.err.log in this folder. Stop with stop.bat.
setlocal enabledelayedexpansion
cd /d "%~dp0"

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
netstat -ano | findstr /r ":8000 *.*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Agent Office is already running on http://127.0.0.1:8000
    start "" http://127.0.0.1:8000/
    exit /b 0
)

if not exist "static\index.html" (
    echo [!] static\ not built yet. Building frontend...
    pushd frontend && call npm run build && popd
    xcopy /e /i /y frontend\dist static >nul || (echo [!] frontend build failed & pause & exit /b 1)
)

echo Starting Agent Office on http://127.0.0.1:8000 (detached; logs: server.log^; stop with stop.bat^) ...
rem run_server.pyw runs console-free (pythonw): closing any window will NOT kill it.
start "" /b "%VENV%\Scripts\pythonw.exe" "%~dp0backend\run_server.pyw"

rem Wait until the server answers (max ~30s). ping is used as a sleep that
rem also works when stdin is redirected.
set /a tries=0
:waitloop
ping -n 3 127.0.0.1 >nul
curl -s -o nul --max-time 3 http://127.0.0.1:8000/
if not errorlevel 1 goto up
set /a tries+=1
if !tries! lss 15 goto waitloop
echo [!] Server did not become healthy within ~30s. See server.err.log in this folder.
exit /b 1

:up
echo Agent Office is up: http://127.0.0.1:8000
echo (Detached - closing windows will NOT stop it. Use stop.bat to stop.)
start "" http://127.0.0.1:8000/
exit /b 0
