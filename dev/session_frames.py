"""Extract review images from a recorded roast session (see record-session.ps1).

Produces, next to the recording in <session>-review/:
  sheet-NNN.jpg     contact sheets, one tile every --every seconds (default 10)
  ev-NNN-*.jpg      a frame shortly after each logged interaction / roast event
  index.json        what each event frame shows

Events come from an interaction log (interactions-*.jsonl, written by the dev build with
ARTISAN_INTERACTION_LOG=1) and/or from the roast's .alog file (works with any Artisan version).

Usage:
  python dev/session_frames.py ~/artisan-sessions/session-20261023-210539.json
      [--interactions ~/artisan-sessions/interactions-....jsonl] [--alog "G:/.../158 ....alog"]
      [--every 10] [--lag 0.6] [--no-sheets]
"""

import argparse
import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

MARKS = ['CHARGE', 'DRY', 'FCs', 'FCe', 'SCs', 'SCe', 'DROP', 'COOL']
ETYPES_DEFAULT = ['Air', 'Drum', 'Damper', 'Burner']
LOGGED_TYPES = {'MouseButtonPress', 'TouchBegin', 'MouseButtonDblClick', 'KeyPress', 'Wheel', 'window_open', 'mark', 'mark_undo', 'state'}


def ffmpeg() -> str:
    exe = shutil.which('ffmpeg')
    if exe is None:
        sys.exit('ffmpeg not found on PATH (run dev/setup-dev.ps1)')
    return exe


def contact_sheets(video: Path, out: Path, every: float) -> None:
    subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(video),
        '-vf', f'fps=1/{every},scale=640:-2,tile=3x3:padding=4:margin=4', '-q:v', '4', str(out / 'sheet-%03d.jpg')], check=True)


def grab(video: Path, offset: float, target: Path) -> None:
    subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', '-ss', f'{max(offset, 0):.2f}', '-i', str(video),
        '-frames:v', '1', '-q:v', '3', str(target)], check=True)


def interaction_events(path: Path) -> list[dict[str, Any]]:
    events = []
    with path.open(encoding='utf-8-sig') as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get('type') not in LOGGED_TYPES:
                continue
            w = rec.get('widget', {})
            label = rec.get('what') or w.get('text') or w.get('tip') or w.get('name') or w.get('class') or ''
            if rec['type'] == 'KeyPress':
                label = f"key {rec.get('key')}"
            events.append({'epoch': rec['epoch'], 'source': 'interactions', 'type': rec['type'], 'label': label,
                'device': rec.get('device'), 'value': w.get('value'), 'roast': rec.get('roast')})
    return events


def alog_events(path: Path) -> list[dict[str, Any]]:
    profile = ast.literal_eval(path.read_text(encoding='utf-8'))
    start = float(profile['roastepoch']) # start of recording, whole seconds
    timex = profile['timex']
    etypes = profile.get('etypes', ETYPES_DEFAULT)
    events = []
    for i, idx in enumerate(profile.get('timeindex', [])[:len(MARKS)]):
        if (i == 0 and idx > -1) or idx > 0:
            events.append({'epoch': start + timex[idx], 'source': 'alog', 'type': 'mark', 'label': MARKS[i]})
    strings = profile.get('specialeventsStrings', [])
    for n, idx in enumerate(profile.get('specialevents', [])):
        et = profile['specialeventstype'][n]
        value = (profile['specialeventsvalue'][n] - 1) * 10
        name = etypes[et] if et < len(etypes) else str(et)
        text = strings[n] if n < len(strings) else ''
        events.append({'epoch': start + timex[idx], 'source': 'alog', 'type': 'event', 'label': f'{name} {value:g} {text}'.strip()})
    return events


def slug(s: str) -> str:
    return ''.join(c if c.isalnum() else '-' for c in s)[:40].strip('-')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('session', type=Path, help='session-*.json written by record-session.ps1')
    ap.add_argument('--interactions', type=Path)
    ap.add_argument('--alog', type=Path)
    ap.add_argument('--every', type=float, default=10, help='seconds between contact sheet tiles')
    ap.add_argument('--lag', type=float, default=0.6, help='seconds after an event to grab its frame (to see the result)')
    ap.add_argument('--no-sheets', action='store_true')
    args = ap.parse_args()

    meta = json.loads(args.session.read_text(encoding='utf-8-sig'))
    video = args.session.parent / meta['video']
    out = args.session.parent / f'{args.session.stem}-review'
    out.mkdir(exist_ok=True)

    if not args.no_sheets:
        contact_sheets(video, out, args.every)

    events: list[dict[str, Any]] = []
    if args.interactions:
        events += interaction_events(args.interactions)
    if args.alog:
        events += alog_events(args.alog)
    events.sort(key=lambda e: e['epoch'])

    index = []
    for n, e in enumerate(events, 1):
        offset = e['epoch'] - meta['start_epoch'] + args.lag
        target = out / f"ev-{n:03d}-{slug(e['type'] + '-' + e['label'])}.jpg"
        grab(video, offset, target)
        index.append({**e, 'video_offset': round(offset, 2), 'frame': target.name})
    (out / 'index.json').write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding='utf-8')
    print(f'{len(index)} event frames and contact sheets written to {out}')


if __name__ == '__main__':
    main()
