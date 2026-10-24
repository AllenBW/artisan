<#
.SYNOPSIS
    Records the screen (and optionally the microphone) during a roast, for workflow analysis.

.DESCRIPTION
    Writes session-<timestamp>.mkv plus a session-<timestamp>.json sidecar holding the wall-clock
    start time to the session folder (default: ~/artisan-sessions, override with -OutDir or
    ARTISAN_SESSION_DIR). A clock is burned into the bottom right corner of the video so frames
    can be matched to the interaction log and to the .alog events.

    Stop the recording by pressing q in this window (Ctrl+C also works; MKV survives an abrupt stop).

.EXAMPLE
    .\dev\record-session.ps1                 # screen + default microphone
    .\dev\record-session.ps1 -NoAudio        # screen only
    .\dev\record-session.ps1 -Fps 10         # smoother, larger files
#>
param(
    [int]$Fps = 5,
    [int]$Width = 1600,                     # output width in pixels, height follows the aspect ratio
    [switch]$NoAudio,
    [string]$Mic = '',                      # DirectShow audio device name; default: first one found
    [string]$OutDir = '',
    [int]$Seconds = 0                       # stop automatically after this many seconds (0: until q is pressed)
)

$ErrorActionPreference = 'Stop'
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'ffmpeg not found. Run dev\setup-dev.ps1 first.' }

if (-not $OutDir) { $OutDir = if ($env:ARTISAN_SESSION_DIR) { $env:ARTISAN_SESSION_DIR } else { Join-Path $HOME 'artisan-sessions' } }
New-Item -ItemType Directory -Force $OutDir | Out-Null

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$video = Join-Path $OutDir "session-$stamp.mkv"
$sidecar = Join-Path $OutDir "session-$stamp.json"

$inputs = @('-f', 'gdigrab', '-framerate', "$Fps", '-draw_mouse', '1', '-i', 'desktop')
if (-not $NoAudio) {
    if (-not $Mic) {
        $listing = & ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1 | Out-String
        $m = [regex]::Match($listing, '"([^"]+)" \(audio\)')
        if ($m.Success) { $Mic = $m.Groups[1].Value } else { Write-Warning 'No microphone found, recording screen only.'; $NoAudio = $true }
    }
    if (-not $NoAudio) { $inputs += @('-f', 'dshow', '-i', "audio=$Mic") }
}

# burned-in wall clock (HH:MM:SS) so every frame carries its own time
$clock = "drawtext=fontfile='C\:/Windows/Fonts/consola.ttf':text='%{localtime\:%H\\\:%M\\\:%S}':x=w-tw-12:y=h-th-12:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6"
$filter = "scale=${Width}:-2,$clock"
$encode = @('-vf', $filter, '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-pix_fmt', 'yuv420p')
if (-not $NoAudio) { $encode += @('-c:a', 'aac', '-b:a', '96k') }
if ($Seconds -gt 0) { $encode += @('-t', "$Seconds") }

$start = [DateTimeOffset]::Now
@{ video = (Split-Path $video -Leaf); start_iso = $start.ToString('o'); start_epoch = $start.ToUnixTimeMilliseconds() / 1000.0;
   fps = $Fps; audio = (-not $NoAudio); mic = $Mic } | ConvertTo-Json | Out-File -Encoding utf8 $sidecar

Write-Host "Recording to $video" -ForegroundColor Green
Write-Host 'Press q in this window to stop.' -ForegroundColor Yellow
& ffmpeg -hide_banner -loglevel warning -stats @inputs @encode $video
Write-Host "Saved $video"
