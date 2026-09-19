@echo off
rem Agent Office - start backend + frontend (served by FastAPI on port 8000)
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] Python venv not found at .venv - create it first:
    echo     python -m venv .venv ^&^& .venv\Scripts\pip install -r backend\requirements.txt
    pause
    exit /b 1
)

if not exist "static\index.html" (
    echo [!] static\ not built yet. Building frontend...
    pushd frontend && call npm run build && popd
    xcopy /e /i /y frontend\dist static >nul || (echo [!] frontend build failed & pause & exit /b 1)
)

echo Starting Agent Office on http://127.0.0.1:8000 ...
.venv\Scripts\python -u -m uvicorn app.main:app --app-dir backend --port 8000
pause
