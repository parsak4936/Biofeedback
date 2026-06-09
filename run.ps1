# ============================================================
#  Biofeedback launcher (PowerShell)
# ============================================================
#  Usage from PowerShell:
#      .\run.ps1
#  If PowerShell complains about ExecutionPolicy, run instead:
#      powershell -ExecutionPolicy Bypass -File .\run.ps1
#  Or just use run.bat (CMD-based; no policy at all).
#
#  This script:
#    1. Pins the working dir to its own location (so it works whether you
#       launch it from PowerShell or by double-clicking).
#    2. Sets ExecutionPolicy to Bypass for THIS PROCESS ONLY (per-shell,
#       per-invocation; does not touch the user's global policy).
#    3. Activates the local virtualenv at env\.
#    4. Runs launcher.py.
# ============================================================

# Stop on first error so we surface problems instead of cascading them.
$ErrorActionPreference = 'Stop'

# 1. cd to the directory this script lives in. $PSScriptRoot is built-in.
Set-Location -Path $PSScriptRoot

# 2. Per-process policy bypass. Scope=Process means it only affects this
#    PowerShell instance and goes away when the shell exits -- safe.
try {
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
} catch {
    Write-Host "[run.ps1] WARN: could not set ExecutionPolicy (already permissive?): $_"
}

# 3. Sanity check + activate the venv.
$activate = Join-Path $PSScriptRoot 'env\Scripts\Activate.ps1'
if (-not (Test-Path $activate)) {
    Write-Host ''
    Write-Host '[run.ps1] ERROR: virtualenv not found at env\Scripts\Activate.ps1'
    Write-Host ''
    Write-Host 'Create one with:'
    Write-Host '    python -m venv env'
    Write-Host '    .\env\Scripts\Activate.ps1'
    Write-Host '    pip install -r requirements.txt'
    Write-Host ''
    Read-Host 'Press Enter to exit'
    exit 1
}

Write-Host '[run.ps1] Activating virtualenv...'
& $activate

# 4. Run the launcher. python.exe is now first on PATH (the venv prepends it).
Write-Host '[run.ps1] Starting biofeedback launcher...'
Write-Host ''
python launcher.py
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    Write-Host ''
    Write-Host "[run.ps1] launcher.py exited with code $rc"
    Read-Host 'Press Enter to exit'
}

exit $rc
