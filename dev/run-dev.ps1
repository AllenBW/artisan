<#
.SYNOPSIS
    Runs Artisan from source with its own settings profile and the interaction log enabled.

.DESCRIPTION
    The dev build keeps its settings under HKCU\Software\artisan-scope\Artisan-Dev, its log under
    %LOCALAPPDATA%\artisan-scope\Artisan-Dev, so it never touches the installed Artisan's settings.
    Interaction logs go to ~/artisan-sessions (override with ARTISAN_SESSION_DIR).

    Close the installed Artisan first: a second running instance opens as the read-only ArtisanViewer.

.EXAMPLE
    .\dev\run-dev.ps1 -CopySettings   # first run: start from a copy of the installed Artisan's settings
    .\dev\run-dev.ps1                 # later runs
    .\dev\run-dev.ps1 -NoLog          # without the interaction log
#>
param(
    [switch]$CopySettings,
    [switch]$NoLog,
    [string]$SettingsProfile = 'Dev',
    [string]$Venv = (Join-Path $env:LOCALAPPDATA 'artisan-dev\.venv')
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No virtual environment at $Venv. Run .\dev\setup-dev.ps1 first." }

$key = "HKCU\Software\artisan-scope\Artisan-$SettingsProfile"
if ($CopySettings) {
    $backups = Join-Path $HOME 'artisan-backups'
    New-Item -ItemType Directory -Force $backups | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    reg export 'HKCU\Software\artisan-scope' (Join-Path $backups "artisan-scope-registry-$stamp.reg") /y | Out-Null
    reg copy 'HKCU\Software\artisan-scope\Artisan' $key /s /f | Out-Null
    Write-Host "Copied installed Artisan settings to $key (backup in $backups)"
}

$env:ARTISAN_SETTINGS_PROFILE = $SettingsProfile
$env:ARTISAN_INTERACTION_LOG = if ($NoLog) { '' } else { '1' }
Push-Location (Join-Path $repo 'src')
try { & $py artisan.py @args } finally { Pop-Location }

