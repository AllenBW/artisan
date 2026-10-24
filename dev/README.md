# Development tools

Windows setup and workflow-analysis tools for this fork.

| Script | What it does |
|---|---|
| `setup-dev.ps1` | Installs Git, uv and FFmpeg with winget, then Python 3.14 (the version the official CI uses) and all dependencies into `%LOCALAPPDATA%\artisan-dev\.venv`. The environment is kept outside OneDrive so packages aren't synced. Safe to re-run. |
| `run-dev.ps1` | Runs Artisan from source with its own settings (`Artisan-Dev`) and the interaction log on. Use `-CopySettings` on the first run to start from a copy of the installed Artisan's settings (a registry backup is made first). Close the installed Artisan before starting it. |
| `record-session.ps1` | Records the screen (and the mic, for narration) to `~/artisan-sessions` with a clock burned into the corner. Press `q` to stop. Use `-NoAudio` for screen only. |
| `session_frames.py` | Turns a recording into contact sheets, plus frames at every tap, mark or slider move. Events come from the interaction log and/or the roast's `.alog` file, so it works with the installed 3.1.0 too. |

## Recording a roast for analysis

```powershell
.\dev\record-session.ps1                       # start before the roast, press q after
& "$env:LOCALAPPDATA\artisan-dev\.venv\Scripts\python.exe" dev\session_frames.py `
    "$HOME\artisan-sessions\session-<stamp>.json" --alog "G:\My Drive\Coffee (1)\FZ94\<roast>.alog"
```

Tip: turn on Windows' touch indicator (Settings > Accessibility > Mouse pointer and touch > Touch indicator) so taps are visible in the recording.

## Interaction log

`ARTISAN_INTERACTION_LOG=1` (set by `run-dev.ps1`) makes the app write `~/artisan-sessions/interactions-<stamp>.jsonl`. It records:
- every tap, click, key press, slider value, dialog open/close and roast mark
- the widget hit and its on-screen size, to judge touch targets
- whether the input came from touch or a mouse

Text typed into input fields is never recorded, only the fact that typing happened.
