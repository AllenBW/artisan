<#
.SYNOPSIS
    Runs Artisan from source with its own settings profile, the interaction log and the MODBUS trace enabled,
    and with -Record the screen recording too.

.DESCRIPTION
    The dev build keeps its settings under HKCU\Software\artisan-scope\Artisan-Dev, its log under
    %LOCALAPPDATA%\artisan-scope\Artisan-Dev, so it never touches the installed Artisan's settings.
    Interaction logs go to ~/artisan-sessions (override with ARTISAN_SESSION_DIR).

    Close the installed Artisan first: a second running instance opens as the read-only ArtisanViewer.

.EXAMPLE
    .\dev\run-dev.ps1 -CopySettings   # first run: start from a copy of the installed Artisan's settings
    .\dev\run-dev.ps1                 # later runs
    .\dev\run-dev.ps1 -NoLog          # without the interaction log
    .\dev\run-dev.ps1 -Record         # also record the screen and the microphone for as long as Artisan is open
#>
param(
    [switch]$CopySettings,
    [switch]$NoLog,
    [switch]$Record,                # screen recording (dev\record-session.ps1), started and stopped with Artisan
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
$env:ARTISAN_MODBUS_TRACE = if ($NoLog) { '' } else { '1' } # wire-level trace of all MODBUS frames
$env:ARTISAN_MCP = '1' # MCP server for Claude on http://127.0.0.1:8765/mcp (read-only towards the roaster)

# the screen recording runs in its own minimized window. Not getting it started must never keep Artisan from starting,
# but is said out loud, as a roast is only roasted once
$rec = $null
$recording = 'off'
$stopFile = Join-Path $env:TEMP "artisan-record-stop-$PID"
if ($Record) {
    try {
        Remove-Item $stopFile -ErrorAction SilentlyContinue
        $rec = Start-Process powershell -PassThru -WindowStyle Minimized -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', "`"$(Join-Path $PSScriptRoot 'record-session.ps1')`"", '-StopFile', "`"$stopFile`""
        Start-Sleep 3 # a recording that cannot start (no ffmpeg, no screen grab) has given up by now
        if ($rec.HasExited) { throw 'the recorder stopped right away' }
        $recording = 'on'
    } catch {
        $rec = $null
        $recording = 'NOT RUNNING'
        Write-Warning "Screen recording not started: $_"
        try {
            (New-Object -ComObject WScript.Shell).Popup("The screen recording did not start ($_).`nArtisan starts without it. To record by hand: dev\record-session.ps1", 15, 'Artisan', 48) | Out-Null
        } catch { }
    }
}
# shown in Artisan's message line at startup: which build this is and what is being captured
try {
    $build = git -C $repo log -1 --format='%h of %cd' --date=format:'%Y-%m-%d %H:%M'
    $logs = if ($NoLog) { 'off' } else { 'on' }
    $env:ARTISAN_BUILD_INFO = "Build $build, interaction log and MODBUS trace $logs, screen recording $recording"
} catch { $env:ARTISAN_BUILD_INFO = '' }

Push-Location (Join-Path $repo 'src')
try { & $py artisan.py @args } finally {
    Pop-Location
    if ($rec) {
        Set-Content $stopFile 'stop'
        if (-not $rec.WaitForExit(20000)) { taskkill /PID $rec.Id /T /F | Out-Null } # MKV survives an abrupt stop
        Remove-Item $stopFile -ErrorAction SilentlyContinue
    }
}

