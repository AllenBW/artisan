<#
.SYNOPSIS
    Sets up (or updates) the Windows development environment for this Artisan fork.

.DESCRIPTION
    - installs Git, uv (Python manager) and FFmpeg via winget
    - installs the Python version used by the official Windows CI build via uv
    - creates a virtual environment OUTSIDE OneDrive (default %LOCALAPPDATA%\artisan-dev\.venv)
      so thousands of package files are not synced to the cloud
    - installs src\requirements.txt and src\requirements-dev.txt into it

    Safe to re-run; already installed tools are skipped.
#>
param(
    [string]$Python = '3.14',
    [string]$Venv = (Join-Path $env:LOCALAPPDATA 'artisan-dev\.venv')
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') }

foreach ($pkg in @(@{ id = 'Git.Git'; cmd = 'git' }, @{ id = 'astral-sh.uv'; cmd = 'uv' }, @{ id = 'Gyan.FFmpeg'; cmd = 'ffmpeg' })) {
    Refresh-Path
    if (Get-Command $pkg.cmd -ErrorAction SilentlyContinue) { Write-Host "$($pkg.cmd) already installed" }
    else { winget install --id $pkg.id -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity }
}
Refresh-Path

uv python install $Python
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) { uv venv $Venv --python $Python }
$py = Join-Path $Venv 'Scripts\python.exe'
uv pip install --python $py -r (Join-Path $repo 'src\requirements.txt') -r (Join-Path $repo 'src\requirements-dev.txt')

& $py -c "import sys, PyQt6.QtCore; print('Python', sys.version.split()[0], '| Qt', PyQt6.QtCore.QT_VERSION_STR)"
Write-Host "Ready. Start the dev build with: .\dev\run-dev.ps1" -ForegroundColor Green
