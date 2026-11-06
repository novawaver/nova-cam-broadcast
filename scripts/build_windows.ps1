# Builds dist\BroadcastServer.exe. FFmpeg is not bundled: the app finds it on
# PATH, next to the exe, in common install folders, or via Settings.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python -or $python.Source -like '*WindowsApps*') {
    throw 'Python 3.11 or newer is required. Install it from python.org.'
}

python -m venv .venv
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .venv\Scripts\python.exe -m pip install --quiet -r windows-server\requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

& .venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --onefile `
    --name BroadcastServer --paths . --paths windows-server `
    windows-server\main.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

Write-Host "Built: $repo\dist\BroadcastServer.exe"
