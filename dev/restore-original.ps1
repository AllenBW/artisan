<#
.SYNOPSIS
    Restores the original Artisan setup saved by backup-original.ps1.

.DESCRIPTION
    Without -Apply this is a dry run that only shows what would change.

    -Apply restores:
      registry  the installed Artisan's settings (keys Artisan and ArtisanViewer are replaced exactly;
                dev/test profiles such as Artisan-Dev are left alone)
      appdata   %LOCALAPPDATA%\artisan-scope\Artisan, mirrored
      roasts    only roast files that are missing or differ in the roast folder; newer roasts are never deleted
    -Program additionally mirrors the program folder back to C:\Program Files\Artisan (needs an elevated shell).
    Before changing anything the current state of the registry is exported to the backup root.

    The backed up program\artisan.exe also runs directly from the backup folder.

.EXAMPLE
    .\dev\restore-original.ps1                                    # dry run with the latest backup
    .\dev\restore-original.ps1 -Apply                             # restore settings, app data, missing roasts
    .\dev\restore-original.ps1 -Apply -Program                    # also the program folder (run as administrator)
#>
param(
    [string]$Backup = '',
    [switch]$Apply,
    [switch]$Program,
    [string]$ProgramDir = 'C:\Program Files\Artisan',
    [string]$RoastDir = 'G:\My Drive\Coffee (1)\FZ94',
    [string]$BackupRoot = (Join-Path $HOME 'artisan-backups')
)

$ErrorActionPreference = 'Stop'
if (-not $Backup) {
    $latest = Get-ChildItem $BackupRoot -Directory -Filter 'original-*' | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) { throw "No backup found in $BackupRoot" }
    $Backup = $latest.FullName
}
if (-not (Test-Path (Join-Path $Backup 'backup.json'))) { throw "$Backup is not a backup made by backup-original.ps1" }
Write-Host "Backup: $Backup" -ForegroundColor Cyan
if (-not $Apply) { Write-Host 'DRY RUN: nothing is changed. Add -Apply to restore.' -ForegroundColor Yellow }
if (Get-Process artisan -ErrorAction SilentlyContinue) { throw 'Close Artisan first (it saves its settings on exit and would overwrite the restore).' }

$list = if ($Apply) { @() } else { @('/L') } # robocopy /L lists without copying

if ($Apply) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    reg export 'HKCU\Software\artisan-scope' (Join-Path $BackupRoot "before-restore-registry-$stamp.reg") /y | Out-Null
}
foreach ($key in 'Artisan', 'ArtisanViewer') {
    $file = Join-Path $Backup "registry\$key.reg"
    Write-Host "registry: replace HKCU\Software\artisan-scope\$key from $key.reg"
    if ($Apply) {
        Remove-Item "HKCU:\Software\artisan-scope\$key" -Recurse -Force -ErrorAction SilentlyContinue
        reg import $file
        if ($LASTEXITCODE -ne 0) { throw "registry import failed for $key" }
    }
}

Write-Host 'appdata:'
robocopy (Join-Path $Backup 'appdata') (Join-Path $env:LOCALAPPDATA 'artisan-scope\Artisan') /MIR /R:1 /W:1 /NJH /NP @list
Write-Host 'roasts (missing or changed files only, nothing deleted):'
robocopy (Join-Path $Backup 'roasts') $RoastDir /E /XO /R:1 /W:1 /NJH /NP @list
if ($Program) {
    Write-Host 'program:'
    robocopy (Join-Path $Backup 'program') $ProgramDir /MIR /R:1 /W:1 /NJH /NP @list
    if ($LASTEXITCODE -ge 8) { throw 'program restore failed (run from an elevated shell)' }
}
Write-Host ($(if ($Apply) { 'Restore complete.' } else { 'Dry run complete.' })) -ForegroundColor Green
