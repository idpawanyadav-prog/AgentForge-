@echo off
rem Agent Office - stop the backend server (all listeners on port 8000)
setlocal
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":8000 *.*LISTENING"') do (
    echo Stopping server PID %%p ...
    taskkill /PID %%p /F >nul 2>&1
    set FOUND=1
)
if not defined FOUND echo Agent Office was not running.
exit /b 0
