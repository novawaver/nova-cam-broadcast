# Builds dist\BroadcastServer.exe. FFmpeg is not bundled: the app finds it on
# PATH, next to the exe, in common install folders, or via Settings.
$ErrorActionPreference = 'Stop'

function Test-Python([string]$Path) {
    # The Microsoft Store stub is on PATH even when Python is not installed.
    if (-not $Path -or $Path -like '*\WindowsApps\*' -or -not (Test-Path $Path)) { return $false }
    & $Path -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
    return $LASTEXITCODE -eq 0
}

function Find-Python {
    # PATH is checked first but cannot be relied on: python.org's installer
    # leaves it off by default, and a shell opened before the install has a
    # stale copy. The registry entries are written by the installer itself.
    $candidates = @()
    $candidates += Get-Command python, python3 -All -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Source }
    foreach ($root in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore') {
        Get-ChildItem $root -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending |
            ForEach-Object {
                $installPath = Join-Path $_.PSPath 'InstallPath'
                $candidates += (Get-ItemProperty $installPath -ErrorAction SilentlyContinue).ExecutablePath
            }
    }
    foreach ($candidate in $candidates) {
        if (Test-Python $candidate) { return $candidate }
    }
    return $null
}

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Find-Python
if (-not $python) {
    throw 'Python 3.11 or newer is required. Install it from python.org.'
}
Write-Host "Using $python"

& $python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .venv\Scripts\python.exe -m pip install --quiet -r windows-server\requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

& .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name BroadcastServer --paths . --paths windows-server `
    windows-server\main.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

Write-Host "Built: $repo\dist\BroadcastServer.exe"
