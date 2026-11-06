# Builds dist\vcam\nvbb_vcam.dll and nvbb-vcam.exe. Needs the Visual Studio C++
# build tools; the Windows client can't provide a camera without these files.
# -Trace logs activation to %ProgramData%\NvidiaBroadcastBridge\vcam.log.
param([switch]$Trace)
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repo 'windows-client\vcam'
$output = Join-Path $repo 'dist\vcam'
$objects = Join-Path $repo 'build\vcam'

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw 'Visual Studio build tools were not found.' }
$install = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $install) { throw 'The Visual Studio C++ tools are not installed.' }

New-Item -ItemType Directory -Force $output, $objects | Out-Null

$flags = "/nologo /std:c++20 /EHsc /O2 /W3 /MT /DUNICODE /D_UNICODE /DWIN32_LEAN_AND_MEAN $(if ($Trace) { '/DNVBB_TRACE' }) /Fo`"$objects\\`""
$dll = "cl $flags /LD `"$source\nvbb_vcam.cpp`" /Fe:`"$output\nvbb_vcam.dll`" /link /DEF:`"$source\nvbb_vcam.def`" /IMPLIB:`"$objects\nvbb_vcam.lib`" mfplat.lib mfuuid.lib mf.lib ole32.lib advapi32.lib uuid.lib strmiids.lib"
$tool = "cl $flags `"$source\vcam_tool.cpp`" /Fe:`"$output\nvbb-vcam.exe`" /link mfplat.lib mf.lib mfreadwrite.lib mfuuid.lib mfsensorgroup.lib ole32.lib uuid.lib"

$script = Join-Path $objects 'build.cmd'
@"
@echo off
call "$install\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
$dll || exit /b 1
$tool || exit /b 1
"@ | Set-Content $script -Encoding ascii

& cmd /c $script
if ($LASTEXITCODE -ne 0) { throw 'The virtual camera build failed.' }
Write-Host "Built: $output"
