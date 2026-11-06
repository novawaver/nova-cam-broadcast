# Tests, builds and assembles everything a release needs into dist\release.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

& powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
if ($LASTEXITCODE -ne 0) { throw 'The Windows build failed.' }

& .venv\Scripts\python.exe -m pip install --quiet pytest
& .venv\Scripts\python.exe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; not creating a release.' }

& .venv\Scripts\python.exe scripts\make_release.py
if ($LASTEXITCODE -ne 0) { throw 'Could not assemble the release.' }
