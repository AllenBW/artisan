<#
.SYNOPSIS
    End-to-end test of the complete capture pipeline against the simulated FZ94 (no roaster needed).

.DESCRIPTION
    1. creates the settings profile Artisan-E2E as a copy of the dev (else the installed) Artisan's settings, with MODBUS pointed
       at the simulator (TCP 127.0.0.1) instead of COM3, sampling every 2 s, and autosave into the test folder;
       aborts if the profile does not point at the simulator
    2. starts the FZ94 simulator (dev/fz94_sim.py)
    3. starts the screen recording
    4. runs Artisan from source, operated by dev/e2e_driver.py through a complete roast
       (interaction log and MODBUS trace enabled)
    5. verifies everything with dev/e2e_verify.py and extracts review frames with dev/session_frames.py

    Results: ~/artisan-sessions/e2e-<timestamp>/ (e2e-report.md, logs, trace, .alog, video, review frames).
    The installed Artisan, its settings and the roast folder are not touched.
#>
param(
    [double]$Speed = 4,
    [int]$Port = 5020,
    [switch]$Maximize,                   # maximize the window, as used at the roaster
    [switch]$Faults,                     # inject faults: BT replies dropped in bursts, the drum 25 command rejected
    [string]$Venv = (Join-Path $env:LOCALAPPDATA 'artisan-dev\.venv')
)

$ErrorActionPreference = 'Stop'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No virtual environment at $Venv. Run .\dev\setup-dev.ps1 first." }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$run = Join-Path (Join-Path $HOME 'artisan-sessions') "e2e-$stamp"
New-Item -ItemType Directory -Force $run | Out-Null

# 1. isolated settings profile pointing at the simulator
$key = 'HKCU\Software\artisan-scope\Artisan-E2E'
Remove-Item 'HKCU:\Software\artisan-scope\Artisan-E2E' -Recurse -Force -ErrorAction SilentlyContinue
# start from the dev profile (it carries the dev build's settings), else from the installed Artisan's
$source = if (Test-Path 'HKCU:\Software\artisan-scope\Artisan-Dev') { 'Artisan-Dev' } else { 'Artisan' }
reg copy "HKCU\Software\artisan-scope\$source" $key /s /f | Out-Null
Remove-Item 'HKCU:\Software\artisan-scope\Artisan-E2E\TouchControls' -Recurse -Force -ErrorAction SilentlyContinue # first start: display preset applied
reg add "$key\Modbus" /v type /t REG_DWORD /d 3 /f | Out-Null
reg add "$key\Modbus" /v host /t REG_SZ /d 127.0.0.1 /f | Out-Null
reg add "$key\Modbus" /v port /t REG_DWORD /d $Port /f | Out-Null
reg add $key /v Delay /t REG_DWORD /d 2000 /f | Out-Null
reg add $key /v autosavepath /t REG_SZ /d $run /f | Out-Null
reg add $key /v profilepath /t REG_SZ /d $run /f | Out-Null
# fresh roast assist state; the roast guide reads (never writes) the real roast history to match backgrounds and targets
Remove-Item 'HKCU:\Software\artisan-scope\Artisan-E2E\RoastAssist' -Recurse -Force -ErrorAction SilentlyContinue
$history = (Get-ItemProperty 'HKCU:\Software\artisan-scope\Artisan' -ErrorAction SilentlyContinue).profilepath
if ($history) { reg add "$key\RoastAssist" /v historyFolder /t REG_SZ /d $history /f | Out-Null }
Remove-ItemProperty 'HKCU:\Software\artisan-scope\Artisan-E2E' -Name lastLoadedProfile -ErrorAction SilentlyContinue
$modbus = Get-ItemProperty "HKCU:\Software\artisan-scope\Artisan-E2E\Modbus"
if ($modbus.type -ne 3 -or $modbus.host -ne '127.0.0.1') { throw 'Safety check failed: the E2E profile does not point MODBUS at the simulator. Aborting.' }
Write-Host "Profile Artisan-E2E -> MODBUS TCP 127.0.0.1:$Port (simulator), results in $run" -ForegroundColor Cyan

# 2. simulator, 3. screen recording
$faultSpec = if ($Faults) { 'drop=25:10:3;reject=22:8193:250' } else { '' }
$simArgs = @((Join-Path $repo 'dev\fz94_sim.py'), '--port', $Port, '--speed', $Speed)
if ($Faults) { $simArgs += @('--drop', '25:10:3', '--reject', '22:8193:250') }
$sim = Start-Process $py -ArgumentList $simArgs -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $run 'simulator.out.log') -RedirectStandardError (Join-Path $run 'simulator.log')
Start-Sleep 2
$seconds = [int](680 / $Speed + 150)
$rec = Start-Process powershell -ArgumentList '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $repo 'dev\record-session.ps1'), '-NoAudio', '-Seconds', $seconds, '-OutDir', $run `
    -PassThru -WindowStyle Hidden
Start-Sleep 2

# 4. the app, operated by the driver
$env:ARTISAN_SETTINGS_PROFILE = 'E2E'
$env:ARTISAN_INTERACTION_LOG = '1'
$env:ARTISAN_MODBUS_TRACE = '1'
$env:ARTISAN_MCP = '1'
# Claude's view: connect to the MCP server mid-roast, call every tool and leave a note in the roast
$probeOut = Join-Path $run 'mcp-probe.json'
$probeCmd = "Start-Sleep 75; & '$py' '$(Join-Path $repo 'dev\mcp_probe.py')' --note 'probe note from e2e' --out '$probeOut'"
$probe = Start-Process powershell -ArgumentList '-NoProfile', '-Command', $probeCmd -PassThru -WindowStyle Hidden
$env:ARTISAN_SESSION_DIR = $run
try {
    $driverArgs = @((Join-Path $repo 'dev\e2e_driver.py'), '--speed', $Speed, '--port', $Port)
    if ($Maximize) { $driverArgs += '--maximize' }
    if ($faultSpec) { $driverArgs += @('--faults', $faultSpec) }
    & $py @driverArgs
    Write-Host "Artisan exited with code $LASTEXITCODE"
} finally {
    Write-Host 'Waiting for the screen recording to finish ...'
    Wait-Process -Id $rec.Id -Timeout ($seconds + 30) -ErrorAction SilentlyContinue
    Stop-Process -Id $sim.Id -Force -ErrorAction SilentlyContinue
    Remove-Item Env:ARTISAN_SETTINGS_PROFILE, Env:ARTISAN_INTERACTION_LOG, Env:ARTISAN_MODBUS_TRACE, Env:ARTISAN_SESSION_DIR, Env:ARTISAN_MCP -ErrorAction SilentlyContinue
    Stop-Process -Id $probe.Id -Force -ErrorAction SilentlyContinue
}

# 5. verification and review frames
& $py (Join-Path $repo 'dev\e2e_verify.py') $run
$verified = $LASTEXITCODE
$session = Get-ChildItem $run -Filter 'session-*.json' | Select-Object -First 1
$alog = Get-ChildItem $run -Filter '*.alog' | Select-Object -First 1
$inter = Get-ChildItem $run -Filter 'interactions-*.jsonl' | Select-Object -First 1
if ($session) {
    $frameArgs = @((Join-Path $repo 'dev\session_frames.py'), $session.FullName, '--every', '10')
    if ($inter) { $frameArgs += @('--interactions', $inter.FullName) }
    if ($alog) { $frameArgs += @('--alog', $alog.FullName) }
    & $py @frameArgs
}
Write-Host "Report: $(Join-Path $run 'e2e-report.md')" -ForegroundColor $(if ($verified -eq 0) { 'Green' } else { 'Red' })
exit $verified
