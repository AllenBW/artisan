# Development tools

Windows setup and workflow-analysis tools for this fork.

| Script | What it does |
|---|---|
| `setup-dev.ps1` | Installs Git, uv and FFmpeg with winget, then Python 3.14 (the version the official CI uses) and all dependencies into `%LOCALAPPDATA%\artisan-dev\.venv`. The environment is kept outside OneDrive so packages aren't synced. Safe to re-run. |
| `run-dev.ps1` | Runs Artisan from source with its own settings (`Artisan-Dev`) and the interaction log on. Use `-CopySettings` on the first run to start from a copy of the installed Artisan's settings (a registry backup is made first). Close the installed Artisan before starting it. |
| `record-session.ps1` | Records the screen (and the mic, for narration) to `~/artisan-sessions` with a clock burned into the corner. Press `q` to stop. Use `-NoAudio` for screen only. |
| `backup-original.ps1` | Preserves the installed Artisan in full (program folder, its registry settings, app data, roast folder) to `~/artisan-backups/original-<stamp>` with a SHA-256 manifest, and verifies every file. |
| `restore-original.ps1` | Restores that backup. Defaults to a dry run; use `-Apply` to restore, and `-Program` (elevated) to also restore the program folder. Never deletes roast files. |
| `fz94_sim.py` | A simulated FZ94: a Modbus TCP server with the roaster's real slave IDs and registers, playing the median logged roast curve. Drives ramp to their setpoints and the heat limit caps ET. |
| `e2e-test.ps1` | End-to-end test of the whole capture pipeline against the simulator, using an isolated `Artisan-E2E` profile (it refuses to start unless Modbus points at the simulator). `e2e_driver.py` operates the app through a full roast; `e2e_verify.py` cross-checks driver ground truth, the interaction log, the Modbus trace, the saved `.alog` and the video, and writes `e2e-report.md`. |
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

Text typed into input fields is never recorded, only the fact that typing happened. Main panels (button rows, sliders, readouts) are recorded as they appear, disappear or move, along with the window geometry.

## Modbus trace

`ARTISAN_MODBUS_TRACE=1` (set by `run-dev.ps1` and `e2e-test.ps1`) writes every frame sent to and received from the machine to `~/artisan-sessions/modbus-<stamp>.jsonl`. Each record has a timestamp, the raw hex, the response latency, and a decode (slave, function, register, count or value, the register values, CRC validity, exceptions). Serial RTU and TCP are decoded; ASCII is logged raw. With the variable unset, nothing changes.

## Notes learned from testing

- Modbus input 1 (slave 25) is **BT**, and input 2 (slave 21) is **ET**. Slave 21 also holds the Drum Heat Limit setpoint (register 50).
- Auto-CHARGE back-dates CHARGE to the actual charge (within about 1 s), but detects it about 4 s later.
- Auto-DROP only works 7 minutes or more into a roast, so a time-compressed test (`-Speed` > 1) always ends with a manual DROP.
- `.alog` event times sit on the sampling grid and its start time is truncated to whole seconds, so they are 0–3 s earlier than the actual tap. The interaction log is accurate to about 0.25 s.
- Each slider release or button tap costs Artisan about 160–200 ms on the UI thread.
- With the current quantifier settings (max 100, against slider maxes of 120 and 50), quantifier events log fan ×1.2 and drum ×0.5, and **move the sliders to those wrong values**.
