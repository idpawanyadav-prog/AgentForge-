# One-time setup for QA browser testing: installs the playwright Python
# package and the headless Chromium build into the AgentForge venv.
$ErrorActionPreference = "Stop"
$Venv = Join-Path $env:USERPROFILE ".verdent\agentforge-venv"
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Py)) { Write-Host "[!] venv not found at $Venv" -ForegroundColor Red; exit 1 }

Write-Host "[*] Installing playwright package..."
& $Py -m pip install playwright

Write-Host "[*] Downloading Chromium (one-time, ~150 MB)..."
& $Py -m playwright install chromium

Write-Host "[OK] Playwright ready. QA browser.test gate is now active."
