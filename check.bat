@echo off
REM Local quality gate: run the backend test suite, then type-check + build
REM the frontend. Exits non-zero on the first failure so it can also be used
REM as a pre-push hook or in CI.
setlocal

set PY=%AGENTFORGE_PY%
if "%PY%"=="" set PY="%USERPROFILE%\.verdent\agentforge-venv\Scripts\python.exe"
if not exist %PY% set PY=python

echo == Backend tests ==
pushd backend
%PY% -m pytest tests -q
set RC=%ERRORLEVEL%
popd
if not "%RC%"=="0" ( echo Backend tests FAILED & exit /b %RC% )

echo == Frontend type-check + build ==
pushd frontend
call npm run build
set RC=%ERRORLEVEL%
popd
if not "%RC%"=="0" ( echo Frontend build FAILED & exit /b %RC% )

echo All checks passed.
endlocal
