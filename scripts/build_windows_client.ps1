# Builds dist\BroadcastClient.exe with the virtual camera inside it. Run
# build_windows.ps1 first, which creates .venv. Needs the Visual Studio C++ tools.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not (Test-Path .venv\Scripts\python.exe)) {
    throw 'Run scripts\build_windows.ps1 first.'
}

& powershell -ExecutionPolicy Bypass -File scripts\build_vcam.ps1
if ($LASTEXITCODE -ne 0) { throw 'The virtual camera build failed.' }

& .venv\Scripts\python.exe -m pip install --quiet -r windows-client\requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

& .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name BroadcastClient --paths . --paths windows-client `
    --add-binary 'dist\vcam\nvbb_vcam.dll;vcam' `
    --add-binary 'dist\vcam\nvbb-vcam.exe;vcam' `
    windows-client\main.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

Write-Host "Built: $repo\dist\BroadcastClient.exe"
