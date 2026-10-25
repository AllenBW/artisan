<#
.SYNOPSIS
    Creates the roast archive: a private git repository with every roast (.alog) and the tasting log, which Artisan
    then commits and pushes to after each save and tasting (artisanlib/roast_archive.py).

.DESCRIPTION
    1. a local repository at -Path (default ~\fz94-roasts; not inside OneDrive or Google Drive, whose syncing can
       corrupt a .git folder), with all roasts from the roast folder in roasts\ and tasting-log.jsonl
    2. a private GitHub repository -Name (with the GitHub CLI; installed with winget and signed in if needed: the
       sign-in happens in your browser), pushed
    3. the settings profiles Artisan-Dev and Artisan-FZ94 pointed at it (RoastArchive/repo)

    The roast folder stays where roasts are saved and shared; the archive keeps every version. Safe to re-run.

.EXAMPLE
    .\dev\setup-roast-archive.ps1
    .\dev\setup-roast-archive.ps1 -NoGitHub      # local repository only
#>
param(
    [string]$Path = (Join-Path $HOME 'fz94-roasts'),
    [string]$Name = 'fz94-roasts',
    [string]$Source = '',             # roast folder; default: the installed Artisan's profile folder
    [switch]$NoGitHub,
    [string[]]$Profiles = @('Dev', 'FZ94')
)

$ErrorActionPreference = 'Stop'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

$full = [IO.Path]::GetFullPath($Path)
foreach ($synced in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial) | Where-Object { $_ }) {
    if ($full.StartsWith([IO.Path]::GetFullPath($synced), [StringComparison]::OrdinalIgnoreCase)) { throw "$full is inside OneDrive: choose a folder that isn't synced." }
}
if ($full -match '\\My Drive\\|\\Google Drive\\') { throw "$full is inside Google Drive: choose a folder that isn't synced." }

if (-not $Source) { $Source = (Get-ItemProperty 'HKCU:\Software\artisan-scope\Artisan' -ErrorAction SilentlyContinue).profilepath }
if (-not $Source -or -not (Test-Path $Source)) { throw "Roast folder not found ($Source): pass -Source." }

# 1. local repository
New-Item -ItemType Directory -Force (Join-Path $full 'roasts') | Out-Null
if (-not (Test-Path (Join-Path $full '.git'))) { git init --quiet --initial-branch main $full }
$readme = Join-Path $full 'README.md'
if (-not (Test-Path $readme)) {
@"
# FZ94 roasts

Every roast profile (``roasts/*.alog``, Artisan's format) and the tasting and grind-setting log (``tasting-log.jsonl``,
one JSON object per line), committed by Artisan after each save and tasting.

The roast folder ($Source) is where roasts are saved and shared; this repository keeps every version.
Nothing is deleted here when a file is removed there.
"@ | Set-Content $readme -Encoding utf8
}
Set-Content (Join-Path $full '.gitattributes') "*.alog -diff`n" -Encoding ascii
$copied = 0
Get-ChildItem $Source -Filter '*.alog' | ForEach-Object {
    $target = Join-Path $full "roasts\$($_.Name)"
    if (-not (Test-Path $target) -or (Get-Item $target).Length -ne $_.Length) { Copy-Item $_.FullName $target; $copied++ }
}
$tasting = Join-Path $Source 'tasting-log.jsonl'
if (Test-Path $tasting) { Copy-Item $tasting (Join-Path $full 'tasting-log.jsonl') }
git -C $full add -A
git -C $full diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    $count = (Get-ChildItem (Join-Path $full 'roasts') -Filter '*.alog').Count
    git -C $full commit --quiet -m "Import $count roasts from the roast folder"
}
Write-Host "Local archive: $full ($copied roasts copied)" -ForegroundColor Green

# 2. private GitHub repository
if (-not $NoGitHub -and -not (git -C $full remote)) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Host 'Installing the GitHub CLI ...'
        winget install --id GitHub.cli -e --silent --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
    $ErrorActionPreference = 'Continue'
    gh auth status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Sign in to GitHub (a browser window opens; the sign-in is yours, nothing is stored by this script):' -ForegroundColor Yellow
        gh auth login --web --git-protocol https
    }
    gh repo create $Name --private --source $full --remote origin --push --description 'FZ94 roast profiles and tasting log (Artisan)'
    if ($LASTEXITCODE -ne 0) { throw 'Creating the GitHub repository failed (see above). The local archive works without it; re-run to try again.' }
    $ErrorActionPreference = 'Stop'
    Write-Host "Private repository: $(gh repo view $Name --json url -q .url)" -ForegroundColor Green
} elseif (git -C $full remote) {
    git -C $full push --quiet --set-upstream origin HEAD
}

# 3. point the dev and roaster builds at it
foreach ($p in $Profiles) {
    $key = "HKCU:\Software\artisan-scope\Artisan-$p"
    if (Test-Path $key) {
        if (-not (Test-Path "$key\RoastArchive")) { New-Item "$key\RoastArchive" | Out-Null }
        Set-ItemProperty "$key\RoastArchive" -Name repo -Value $full
        Write-Host "Artisan-$p commits roasts to $full"
    }
}
