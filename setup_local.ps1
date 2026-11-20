$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.\.venv\Scripts\python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 virtual environment creation failed.' }
}
$env:PIP_CONFIG_FILE = 'NUL'
$env:PIP_INDEX_URL = 'https://pypi.org/simple'
$env:PIP_EXTRA_INDEX_URL = ''
$dependencies = if (Test-Path '.\requirements-lock.txt') { 'requirements-lock.txt' } else { 'requirements.txt' }
& '.\.venv\Scripts\python.exe' -m pip install -r $dependencies --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path '.\.env')) {
    Copy-Item '.\.env.example' '.\.env'
    Write-Host 'Set your credentials and dedicated DATABASE_URL in .env, then run this script again.'
    exit 0
}
& '.\.venv\Scripts\python.exe' '.\scripts\bootstrap.py'
if ($LASTEXITCODE -ne 0) { throw 'Local database initialization failed.' }
New-Item -ItemType Directory -Path '.\.local\bin' -Force | Out-Null
if (-not (Test-Path '.\.local\bin\minio.exe')) {
    Invoke-WebRequest -Uri 'https://dl.min.io/server/minio/release/windows-amd64/minio.exe' -OutFile '.\.local\bin\minio.exe'
}
& '.\.venv\Scripts\python.exe' -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
Write-Host 'Setup finished. Run .\run.ps1 to start the application.'
