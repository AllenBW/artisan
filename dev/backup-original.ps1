<#
.SYNOPSIS
    Preserves the original, installed Artisan setup so it can be restored in its entirety.

.DESCRIPTION
    Copies into ~/artisan-backups/original-<timestamp>/ :
      program\    the installed Artisan folder (self-contained; artisan.exe runs from the copy)
      registry\   Artisan.reg and ArtisanViewer.reg (the installed app's settings only, not the dev profile)
      appdata\    %LOCALAPPDATA%\artisan-scope\Artisan (logs, local databases)
      roasts\     the roast profile folder (.alog files and docs)
    then writes manifest.csv (SHA-256 of every file) and verifies every copied file against its source.
    Restore with dev\restore-original.ps1.
#>
param(
    [string]$ProgramDir = 'C:\Program Files\Artisan',
    [string]$RoastDir = 'G:\My Drive\Coffee (1)\FZ94',
    [string]$BackupRoot = (Join-Path $HOME 'artisan-backups')
)

$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dest = Join-Path $BackupRoot "original-$stamp"
New-Item -ItemType Directory -Force $dest | Out-Null

$running = Get-Process artisan -ErrorAction SilentlyContinue
if ($running) { Write-Warning 'The installed Artisan is running; its settings are saved on exit. Close it for a fully consistent backup.' }

$sources = [ordered]@{
    program = $ProgramDir
    appdata = Join-Path $env:LOCALAPPDATA 'artisan-scope\Artisan'
    roasts  = $RoastDir
}
foreach ($name in $sources.Keys) {
    $src = $sources[$name]
    if (-not (Test-Path $src)) { Write-Warning "missing: $src"; continue }
    Write-Host "Copying $src ..."
    robocopy $src (Join-Path $dest $name) /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $src (exit $LASTEXITCODE)" }
}

$reg = Join-Path $dest 'registry'
New-Item -ItemType Directory -Force $reg | Out-Null
foreach ($key in 'Artisan', 'ArtisanViewer') {
    reg export "HKCU\Software\artisan-scope\$key" (Join-Path $reg "$key.reg") /y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "registry export failed for $key" }
}

# manifest and verification: every backed up file must match its source byte for byte
Write-Host 'Hashing and verifying ...'
$rows = New-Object System.Collections.Generic.List[object]
$mismatches = 0
foreach ($name in $sources.Keys) {
    $src = $sources[$name]
    if (-not (Test-Path $src)) { continue }
    $srcFiles = Get-ChildItem $src -Recurse -File -Force
    foreach ($f in $srcFiles) {
        $rel = $f.FullName.Substring($src.TrimEnd('\').Length + 1)
        $copy = Join-Path (Join-Path $dest $name) $rel
        $h1 = (Get-FileHash $f.FullName -Algorithm SHA256).Hash
        $h2 = if (Test-Path -LiteralPath $copy) { (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash } else { 'MISSING' }
        if ($h1 -ne $h2) { $mismatches++; Write-Warning "mismatch: $name\$rel" }
        $rows.Add([pscustomobject]@{ area = $name; path = $rel; bytes = $f.Length; sha256 = $h2; source_sha256 = $h1 })
    }
}
$rows | Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $dest 'manifest.csv')

$summary = [ordered]@{
    created = (Get-Date).ToString('o'); computer = $env:COMPUTERNAME
    artisan_version = (Get-Item (Join-Path $ProgramDir 'artisan.exe')).VersionInfo.ProductVersion
    installed_artisan_running = [bool]$running
    files = $rows.Count; bytes = ($rows | Measure-Object bytes -Sum).Sum; mismatches = $mismatches
    per_area = @($sources.Keys | ForEach-Object { $a = $_; [ordered]@{ area = $a; files = @($rows | Where-Object area -eq $a).Count } })
    registry = @('Artisan.reg', 'ArtisanViewer.reg' | ForEach-Object { [ordered]@{ file = $_; bytes = (Get-Item (Join-Path $reg $_)).Length } })
}
$summary | ConvertTo-Json -Depth 4 | Out-File -Encoding utf8 (Join-Path $dest 'backup.json')
$summary | ConvertTo-Json -Depth 4
if ($mismatches -gt 0) { throw "$mismatches files did not verify" }
Write-Host "Backup verified: $dest" -ForegroundColor Green
