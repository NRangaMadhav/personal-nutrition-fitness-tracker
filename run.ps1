$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.\.venv\Scripts\python.exe')) {
    throw 'Project virtual environment is missing. Run setup_local.ps1 first.'
}
& '.\.venv\Scripts\python.exe' '.\scripts\run_local.py'
if ($LASTEXITCODE -ne 0) { throw 'Tracker launcher failed. Check .local logs.' }
