<#
.SYNOPSIS
    Safe update of the roaster build: builds a release from a commit, tests it end to end against the simulator, and
    only then makes it the one the "Artisan FZ94" shortcut starts. The previous release is kept for -Rollback.

.DESCRIPTION
    Releases live outside OneDrive in %LOCALAPPDATA%\artisan-fork:
        r\<commit>\          a git worktree of this repository at that commit, with its own .venv
        current.txt          the release the shortcut starts
        previous.txt         the release before it (-Rollback goes back to it)
        Artisan-FZ94.cmd     the launcher (settings profile Artisan-FZ94, MCP server, logs and MODBUS trace on)

    Steps: fetch (with -Pull), check out the commit as a new worktree, install its requirements with uv, run
    dev\e2e-test.ps1 from the release (normal, then with faults unless -Quick) using a copy of the FZ94 settings
    pointed at the simulator, and switch current.txt only if every check passed. The roaster is never contacted.

    The first update creates the settings profile Artisan-FZ94 as a copy of Artisan-Dev (registry backup first) and a
    desktop shortcut "Artisan FZ94". The installed Artisan and its settings are not touched.

    The tests take over the screen for about 10 minutes (about 5 with -Quick): don't use the computer meanwhile.

.EXAMPLE
    .\dev\update.ps1                 # release the commit checked out here (HEAD), after testing it
    .\dev\update.ps1 -Pull           # fetch origin and release origin/<this branch>
    .\dev\update.ps1 -Ref 1a2b3c4d   # release a specific commit
    .\dev\update.ps1 -Rollback       # go back to the previous release
    .\dev\update.ps1 -Status         # show the current and previous release
#>
param(
    [string]$Ref = 'HEAD',
    [switch]$Pull,
    [switch]$Quick,                 # one E2E run (no fault run)
    [switch]$SkipTests,             # release without testing (not recommended)
    [switch]$Rollback,
    [switch]$Status,
    [switch]$Yes,                   # don't wait for Enter before the tests take over the screen
    [string]$SettingsProfile = 'FZ94',
    [int]$Keep = 3                  # releases kept on disk (the current and previous are always kept)
)

$ErrorActionPreference = 'Stop'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$repo = Split-Path -Parent $PSScriptRoot
$root = Join-Path $env:LOCALAPPDATA 'artisan-fork'
$releases = Join-Path $root 'r'
$currentFile = Join-Path $root 'current.txt'
$previousFile = Join-Path $root 'previous.txt'
New-Item -ItemType Directory -Force $releases | Out-Null

function Read-Release([string]$file) { if (Test-Path $file) { (Get-Content $file -Raw).Trim() } else { '' } }
function Describe([string]$dir) {
    if (-not $dir -or -not (Test-Path $dir)) { return '(none)' }
    $line = git -C $dir log -1 --format='%h %cd %s' --date=format:'%Y-%m-%d %H:%M'
    return "$line  [$dir]"
}

if ($Status) {
    Write-Host "current : $(Describe (Read-Release $currentFile))"
    Write-Host "previous: $(Describe (Read-Release $previousFile))"
    exit 0
}

if ($Rollback) {
    $prev = Read-Release $previousFile
    if (-not $prev -or -not (Test-Path $prev)) { throw 'No previous release to roll back to.' }
    $cur = Read-Release $currentFile
    Set-Content $currentFile $prev -Encoding ascii
    if ($cur) { Set-Content $previousFile $cur -Encoding ascii }
    Write-Host "Rolled back. Current: $(Describe $prev)" -ForegroundColor Green
    exit 0
}

# 1. the commit to release
if ($Pull) {
    git -C $repo fetch --quiet origin
    if ($LASTEXITCODE -ne 0) { throw 'git fetch failed.' }
    if ($Ref -eq 'HEAD') { $Ref = 'origin/' + (git -C $repo rev-parse --abbrev-ref HEAD) }
}
$sha = git -C $repo rev-parse --verify "$Ref^{commit}"
if ($LASTEXITCODE -ne 0 -or -not $sha) { throw "Unknown commit: $Ref" }
$short = $sha.Substring(0, 10)
$dir = Join-Path $releases $short
if ((Read-Release $currentFile) -eq $dir) { Write-Host "Already the current release: $(Describe $dir)"; exit 0 }
if ($Ref -eq 'HEAD' -and (git -C $repo status --porcelain)) {
    Write-Host 'Note: uncommitted changes in the checkout are not part of the release (only committed work is released).' -ForegroundColor Yellow
}

# 2. the release: worktree + its own environment
if (-not (Test-Path (Join-Path $dir 'src\artisan.py'))) {
    Write-Host "Creating release $short ..." -ForegroundColor Cyan
    git -C $repo worktree prune
    git -C $repo worktree add --quiet --detach $dir $sha
    if ($LASTEXITCODE -ne 0) { throw 'git worktree add failed.' }
}
$venv = Join-Path $dir '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw 'uv not found: run .\dev\setup-dev.ps1 first.' }
if (-not (Test-Path $py)) { & $uv venv $venv --python 3.14 --quiet; if ($LASTEXITCODE -ne 0) { throw 'uv venv failed.' } }
Write-Host 'Installing requirements ...' -ForegroundColor Cyan
& $uv pip install --quiet --compile-bytecode --python $py -r (Join-Path $dir 'src\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing the requirements failed: the current release is unchanged.' }

# 3. the production settings profile (first update only)
$profileKey = "HKCU:\Software\artisan-scope\Artisan-$SettingsProfile"
if (-not (Test-Path $profileKey)) {
    $from = if (Test-Path 'HKCU:\Software\artisan-scope\Artisan-Dev') { 'Artisan-Dev' } else { 'Artisan' }
    $backups = Join-Path $HOME 'artisan-backups'
    New-Item -ItemType Directory -Force $backups | Out-Null
    reg export 'HKCU\Software\artisan-scope' (Join-Path $backups "artisan-scope-registry-$(Get-Date -Format 'yyyyMMdd-HHmmss').reg") /y | Out-Null
    reg copy "HKCU\Software\artisan-scope\$from" "HKCU\Software\artisan-scope\Artisan-$SettingsProfile" /s /f | Out-Null
    Write-Host "Created settings profile Artisan-$SettingsProfile from $from (registry backup in $backups)"
}

# 4. end-to-end tests of the release, against the simulator, with a copy of the production settings
if (-not $SkipTests) {
    if (-not $Yes) {
        Write-Host 'The tests now take over the screen (about 5 minutes per run). Don''t use the computer meanwhile.' -ForegroundColor Yellow
        Read-Host 'Press Enter to start'
    }
    $runs = if ($Quick) { @('normal') } else { @('normal', 'faults') }
    foreach ($run in $runs) {
        Write-Host "E2E ($run) ..." -ForegroundColor Cyan
        $e2eArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $dir 'dev\e2e-test.ps1'), '-Maximize', '-Venv', $venv,
            '-SourceProfile', "Artisan-$SettingsProfile")
        if ($run -eq 'faults') { $e2eArgs += '-Faults' }
        & powershell @e2eArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Host "E2E failed: release $short NOT installed; the current release is unchanged. See the report above." -ForegroundColor Red
            exit 1
        }
    }
}

# 5. switch, launcher, shortcut, cleanup
$cur = Read-Release $currentFile
if ($cur -and $cur -ne $dir) { Set-Content $previousFile $cur -Encoding ascii }
Set-Content $currentFile $dir -Encoding ascii

$launcher = Join-Path $root 'Artisan-FZ94.cmd'
@"
@echo off
rem Starts the current release of the roaster build (see dev\update.ps1 in the repository).
set /p REL=<"%LOCALAPPDATA%\artisan-fork\current.txt"
start "Artisan FZ94" /min powershell -NoProfile -ExecutionPolicy Bypass -File "%REL%\dev\run-dev.ps1" -SettingsProfile $SettingsProfile -Venv "%REL%\.venv"
"@ | Set-Content $launcher -Encoding ascii

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Artisan FZ94.lnk'
if (-not (Test-Path $lnk)) {
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $launcher
    $s.WorkingDirectory = $root
    $icon = Join-Path $dir 'src\artisan.ico'
    if (Test-Path $icon) { $s.IconLocation = $icon }
    $s.Description = 'Artisan, roaster build (current tested release)'
    $s.Save()
    Write-Host "Desktop shortcut: $lnk"
}

$keepDirs = @((Read-Release $currentFile), (Read-Release $previousFile))
# a file held open (eg. by OneDrive syncing the repository) makes git ask "Should I try again? (y/n)": never wait on
# that, the old release is removed on a later update instead
$env:GIT_ASK_YESNO = 'false'
Get-ChildItem $releases -Directory | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep |
    Where-Object { $keepDirs -notcontains $_.FullName } | ForEach-Object {
        git -C $repo worktree remove --force $_.FullName
        if ($LASTEXITCODE -eq 0) { Write-Host "Removed old release $($_.Name)" }
        else { Write-Warning "Old release $($_.Name) not removed (in use); retried on the next update" }
    }

Write-Host "Updated. Current: $(Describe $dir)" -ForegroundColor Green
if ($cur) { Write-Host "Previous (for .\dev\update.ps1 -Rollback): $(Describe $cur)" }
