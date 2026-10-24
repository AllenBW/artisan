<#
.SYNOPSIS
    Applies the settings fixes found by the roast log analysis and the end-to-end test.

.DESCRIPTION
    1. Quantifier scaling (all given profiles): each active quantifier's max is set to its slider's max.
       The drive readbacks are in slider units (fan 0-120, drum 0-50), but quantifiermax was the default 100,
       so quantified events were logged as fan x1.2 and drum x0.5, and the sliders were moved to those wrong values.
    2. Sampling interval (dev profile only): 2 s, as the installed Artisan 3.1.0 samples by default
       (Artisan 4.2.1 defaults to 1 s).

    Artisan must be closed (it saves its settings on exit). The registry is exported to ~/artisan-backups first.
#>
param(
    [string[]]$Profiles = @('Artisan', 'Artisan-Dev'),
    [string]$DevProfile = 'Artisan-Dev'
)

$ErrorActionPreference = 'Stop'
$running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'artisan.exe' -or ($_.Name -eq 'python.exe' -and $_.CommandLine -match 'artisan\.py|e2e_driver\.py') }
if ($running) { throw 'Close Artisan first (it saves its settings on exit and would overwrite these changes).' }

$backups = Join-Path $HOME 'artisan-backups'
New-Item -ItemType Directory -Force $backups | Out-Null
$backup = Join-Path $backups "before-fix-settings-$(Get-Date -Format 'yyyyMMdd-HHmmss').reg"
reg export 'HKCU\Software\artisan-scope' $backup /y | Out-Null
Write-Host "Registry backup: $backup"

foreach ($name in $Profiles) {
    $root = "HKCU:\Software\artisan-scope\$name"
    if (-not (Test-Path $root)) { Write-Warning "no settings profile $name"; continue }
    $sliders = Get-ItemProperty "$root\Sliders" -ErrorAction SilentlyContinue
    $quant = Get-ItemProperty "$root\Quantifiers" -ErrorAction SilentlyContinue
    $slidermax = if ($sliders.slidermax) { @($sliders.slidermax | ForEach-Object { [int]$_ }) } else { @(100, 100, 100, 100) }
    $active = if ($quant.quantifieractive) { @($quant.quantifieractive | ForEach-Object { [int]$_ }) } else { @(0, 0, 0, 0) }
    $before = if ($quant.quantifiermax) { @($quant.quantifiermax) } else { @('100', '100', '100', '100') }
    $after = @(0..3 | ForEach-Object { if ($active[$_]) { "$($slidermax[$_])" } else { $before[$_] } })
    if (-not (Test-Path "$root\Quantifiers")) { New-Item "$root\Quantifiers" | Out-Null }
    Set-ItemProperty "$root\Quantifiers" -Name quantifiermax -Type MultiString -Value $after
    Write-Host ("{0}: quantifiermax {1} -> {2} (slidermax {3}, active {4})" -f $name, ($before -join ','), ($after -join ','), ($slidermax -join ','), ($active -join ','))
}

$dev = "HKCU:\Software\artisan-scope\$DevProfile"
if (Test-Path $dev) {
    $old = (Get-ItemProperty $dev).Delay
    Set-ItemProperty $dev -Name Delay -Type DWord -Value 2000
    Write-Host ("{0}: sampling interval {1} -> 2000 ms" -f $DevProfile, $(if ($old) { "$old ms" } else { 'default (1000 ms)' }))
}
Write-Host 'Done.' -ForegroundColor Green
