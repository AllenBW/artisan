"""Verifies that an end-to-end test session (dev/e2e-test.ps1) was captured completely and consistently.

Cross-checks four independent records of the same session:
  driver-*.jsonl        what the test driver actually did (ground truth)
  interactions-*.jsonl  what the interaction log captured
  modbus-*.jsonl        every MODBUS frame on the wire
  *.alog                the roast profile Artisan saved
plus the screen recording (session-*.json / .mkv).

Writes e2e-report.md and e2e-report.json into the session folder; exit code 1 if any check failed.

Usage: python dev/e2e_verify.py <session folder>
"""

import ast
import json
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MARKS = ['CHARGE', 'DRY', 'FCs', 'FCe', 'SCs', 'SCe', 'DROP', 'COOL']
# extra event buttons and the MODBUS write each must cause: label -> (slave, register, value)
BUTTON_WRITES = {'Fan On': (23, 8192, 2), 'Fan Off': (23, 8192, 1), 'Drum On': (22, 8192, 2), 'Drum Off': (22, 8192, 1)}
# sliders and their write: name -> (slave, register, factor)
SLIDER_WRITES = {'fan slider': (23, 8193, 10), 'drum slider': (22, 8193, 10), 'heat slider': (21, 50, 1)}
# channels that must be read every sampling cycle: (slave, function, register)
# (MODBUS input 1 = slave 25 is BT, input 2 = slave 21 is ET; Artisan's MODBUS device returns them in this order)
CHANNELS = {'BT': (25, 4, 1000), 'ET': (21, 4, 1000), 'Exhaust': (24, 4, 1000), 'Heat limit': (21, 3, 50),
    'Drum drive': (22, 3, 2), 'Fan drive': (23, 3, 2)}
# Artisan stamps a manual event with the time of the last sample (up to one sampling interval earlier) and stores the
# roast start (roastepoch) in whole seconds (truncated), so an .alog event time is expected this far from the tap
MANUAL_MARK_WINDOW = (-3.5, 0.5) # seconds, for 2 s sampling
# the Windows 11 taskbar covers the bottom 48 logical pixels of the screen when it is shown over a window
TASKBAR_PX = 48


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding='utf-8-sig') as f:
        return [json.loads(line) for line in f if line.strip()]


# finds a tool on PATH, also on the current PATH from the registry (tools installed after this process' shell started)
def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found is None and sys.platform == 'win32':
        import winreg
        paths = []
        for root, sub in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
            try:
                with winreg.OpenKey(root, sub) as key:
                    paths.append(winreg.QueryValueEx(key, 'Path')[0])
            except OSError:
                pass
        import os
        found = shutil.which(name, path=os.path.expandvars(';'.join(paths)))
    return found


def one(folder: Path, pattern: str) -> Path | None:
    files = sorted(folder.glob(pattern))
    return files[-1] if files else None


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, area: str, what: str, ok: bool, detail: str = '') -> None:
        self.checks.append({'area': area, 'check': what, 'ok': bool(ok), 'detail': detail})

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c['ok']]


def norm(text: str) -> str:
    return ' '.join(text.replace('&', '').split()).lower()


def main() -> None:
    folder = Path(sys.argv[1])
    rep = Report()
    stats: dict[str, Any] = {}

    driver_p, inter_p, modbus_p = one(folder, 'driver-*.jsonl'), one(folder, 'interactions-*.jsonl'), one(folder, 'modbus-*.jsonl')
    alog_p, session_p = one(folder, '*.alog'), one(folder, 'session-*.json')
    for name, p in [('driver log', driver_p), ('interaction log', inter_p), ('MODBUS trace', modbus_p), ('roast profile (.alog)', alog_p), ('screen recording', session_p)]:
        rep.check('files', f'{name} present', p is not None, p.name if p else 'missing')
    if not (driver_p and inter_p and modbus_p):
        finish(folder, rep, stats)
        return

    driver, inter, trace = load_jsonl(driver_p), load_jsonl(inter_p), load_jsonl(modbus_p)
    t0 = min(r['epoch'] for r in driver)
    t_end = max(r['epoch'] for r in driver)
    stats['session_seconds'] = round(t_end - t0, 1)
    errors = [r for r in driver if r['action'] == 'driver_error']
    rep.check('driver', 'script ran without errors', not errors, '; '.join(r['error'] for r in errors))
    for r in driver:
        if r['action'] == 'check':
            rep.check('driver', r['what'], r['ok'])
    stats['marks_auto'] = {r['mark']: r['action'] == 'mark_seen' for r in driver if r['action'] in {'mark_seen', 'mark_missing'}}
    ready = next((r for r in driver if r['action'] == 'app_ready'), {})
    stats['speed'], stats['maximized'] = ready.get('speed'), ready.get('maximized')

    # 1. every tap and drag of the driver is in the interaction log, on the right widget, with the right device
    presses = [r for r in inter if r['type'] == 'MouseButtonPress']
    releases = [r for r in inter if r['type'] == 'MouseButtonRelease']
    for a in (r for r in driver if r['action'] == 'tap'):
        hit = sorted((p for p in presses if abs(p['epoch'] - a['epoch']) < 0.5 and norm(p['widget'].get('text', '')) == norm(a['text'])),
            key=lambda p: abs(p['epoch'] - a['epoch']))
        ok = bool(hit) and hit[0]['device'] == a['device']
        rep.check('interaction log', f"tap {a['target']} ({a['device']})", ok,
            f"logged as {hit[0]['device']} on {hit[0]['widget']['class']} {hit[0]['widget'].get('rect')}" if hit else 'not logged')
    drag_start: dict[str, Any] = {}
    for a in driver:
        if a['action'] == 'drag_start':
            drag_start = a
        if a['action'] != 'drag_end':
            continue
        # the release is logged when delivered, before Artisan handles it (redraw, event, MODBUS write), so it lies within the drag
        rel = [p for p in releases if drag_start['epoch'] - 0.01 <= p['epoch'] <= a['epoch'] and 'Slider' in p['widget']['class']][:1]
        ok = bool(rel) and rel[0]['widget'].get('value') == a['value']
        rep.check('interaction log', f"drag {a['target']} to {a['value']}", ok, f"logged value {rel[0]['widget'].get('value')}" if rel else 'not logged')
    # a slider keeps the value it was last set to (quantifiers must not move it to a different value)
    last_set: dict[str, int] = {}
    for a in driver:
        if a['action'] == 'drag_start' and a['target'] in last_set:
            rep.check('sliders', f"{a['target']} still at {last_set[a['target']]} before the next move", a['value'] == last_set[a['target']],
                f"found {a['value']}")
        if a['action'] in {'drag_end', 'set'}:
            last_set[a['target']] = a['value']

    # the custom buttons and the event buttons stay visible and on screen while the roaster is in use
    screens = next((r.get('screens') for r in inter if r['type'] == 'session_start'), None) or []
    screen_h = max((s['geometry'][1] + s['geometry'][3] for s in screens), default=None)
    t_on = next((r['epoch'] for r in inter if r['type'] == 'state' and r['what'] == 'ON'), None)
    t_off = next((r['epoch'] for r in inter if r['type'] == 'state' and r['what'] == 'OFF'), None)
    t_start = next((r['epoch'] for r in inter if r['type'] == 'state' and r['what'] == 'START'), None)
    t_stop = next((r['epoch'] for r in inter if r['type'] == 'state' and r['what'] == 'STOP'), None)
    for panel, label, since, until in (('extrabuttondialogs', 'Fan/Drum buttons', t_on, t_off),
            ('lowerbuttondialog', 'CHARGE..DROP buttons', t_start, t_stop)): # the event buttons are only shown while recording
        if since is None or until is None:
            continue
        state = None
        problems = []
        for r in (r for r in inter if r['type'] == 'panel' and r['what'] == panel):
            state = r
            if since <= r['epoch'] < until:
                if not r['visible']:
                    problems.append(f"hidden at +{r['epoch'] - t0:.0f} s")
                elif screen_h is not None and r['rect'] and r['rect'][1] + r['rect'][3] > screen_h:
                    problems.append(f"off screen at +{r['epoch'] - t0:.0f} s ({r['rect']})")
                elif screen_h is not None and r['rect'] and r['rect'][1] + r['rect'][3] > screen_h - TASKBAR_PX:
                    problems.append(f"in the taskbar zone (bottom {TASKBAR_PX} px) at +{r['epoch'] - t0:.0f} s ({r['rect']})")
        if state is not None and state['epoch'] < since and not state['visible']:
            problems.append('hidden when sampling started')
        rep.check('layout', f'{label} visible and on screen while in use', not problems, '; '.join(problems) or f"last placement {state['rect'] if state else None}")

    states = [r['what'] for r in inter if r['type'] == 'state']
    rep.check('interaction log', 'ON / START / OFF states', all(s in states for s in ('ON', 'START', 'OFF')), ', '.join(states))
    marks = {r['what']: r['epoch'] for r in inter if r['type'] == 'mark'}
    for m in ('CHARGE', 'DRY', 'FCs', 'DROP'):
        rep.check('interaction log', f'{m} mark', m in marks)
    for m, target in (('DRY', 'DRY'), ('FCs', 'FCs')):
        tap = next((r['epoch'] for r in driver if r['action'] == 'tap' and r['target'] == target), None)
        if tap and m in marks:
            rep.check('interaction log', f'{m} mark within 1 s of its tap', abs(marks[m] - tap) < 1.0, f'{marks[m] - tap:+.2f} s')
    panels = [r for r in inter if r['type'] == 'panel']
    rep.check('interaction log', 'panel visibility recorded', bool(panels), f'{len(panels)} panel records')
    stats['panel_changes'] = [(round(r['epoch'] - t0, 1), r['what'], r['visible']) for r in panels]

    # 2. MODBUS trace: decodes cleanly, every request answered, every channel read every cycle, writes match actions
    tx = [r for r in trace if r.get('dir') == 'tx']
    rx = [r for r in trace if r.get('dir') == 'rx']
    partial = [r for r in trace if r.get('partial')]
    rep.check('MODBUS trace', 'all frames decoded', not partial, f'{len(tx)} sent, {len(rx)} received, {len(partial)} undecoded')
    # injected faults (simulator): dropped requests to a slave, rejected writes
    faults = next((r.get('faults') or '' for r in driver if r['action'] == 'app_ready'), '')
    dropped_slaves = {int(f.split('=', 1)[1].split(':')[0]) for f in faults.split(';') if f.startswith('drop=')}
    rejected = {tuple(int(x) for x in f.split('=', 1)[1].split(':')) for f in faults.split(';') if f.startswith('reject=')}
    stats['faults'] = faults
    exceptions = [r for r in rx if 'exception' in r]
    unexpected_exc = [r for r in exceptions if not any(r['slave'] == s and r['fc'] == 6 for s, _, _ in rejected)]
    rep.check('MODBUS trace', 'no unexpected exception responses', not unexpected_exc, f'{len(exceptions)} exceptions, {len(unexpected_exc)} unexpected')
    # a request is answered if the next frame on the wire, before the next request, is a reply from the same slave
    # (works for RTU, which has no transaction id, and for TCP)
    frames = [r for r in trace if r.get('dir') in {'tx', 'rx'} and not r.get('noise')]
    unanswered = [f for i, f in enumerate(frames) if f['dir'] == 'tx' and not (i + 1 < len(frames) and frames[i+1]['dir'] == 'rx' and frames[i+1].get('slave') == f.get('slave'))]
    unexpected_miss = [f for f in unanswered if f.get('slave') not in dropped_slaves]
    rep.check('MODBUS trace', 'every request answered (except injected drops)', not unexpected_miss,
        f"{len(unanswered)} unanswered, from slaves {sorted({f.get('slave') for f in unanswered})}")
    if dropped_slaves:
        rep.check('MODBUS trace', 'injected drops happened', bool(unanswered), f'{len(unanswered)} unanswered requests')
    latencies = [r['ms'] for r in rx if 'ms' in r]
    if latencies:
        stats['latency_ms'] = {'median': statistics.median(latencies), 'max': max(latencies)}
    reads = defaultdict(list)
    for r in tx:
        if r.get('fc') in {3, 4} and 'count' in r:
            for reg in range(r['register'], r['register'] + r['count']):
                reads[(r['slave'], r['fc'], reg)].append(r['epoch'])
    for name, key in CHANNELS.items():
        times = reads.get(key, [])
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        detail = f'{len(times)} reads, interval median {statistics.median(gaps):.2f} s, max {max(gaps):.2f} s' if gaps else f'{len(times)} reads'
        rep.check('MODBUS trace', f'{name} read every cycle', len(times) > 10 and (not gaps or max(gaps) < 3 * statistics.median(gaps)), detail)
    stats['sampling_interval_s'] = round(statistics.median([b - a for a, b in zip(reads[CHANNELS['BT']], reads[CHANNELS['BT']][1:], strict=False)]), 3) if len(reads[CHANNELS['BT']]) > 1 else None
    writes = [r for r in tx if r.get('fc') in {6, 16}]
    stats['writes'] = [(round(r['epoch'] - t0, 1), r['slave'], r['register'], r.get('value', r.get('values'))) for r in writes]
    def written(slave: int, register: int, value: int, after: float, within: float = 2.0) -> bool:
        return any(r['slave'] == slave and r['register'] == register and (r.get('value') == value or value in (r.get('values') or []))
            and after - 0.2 <= r['epoch'] <= after + within for r in writes)
    for a in (r for r in driver if r['action'] == 'tap' and r['target'] in BUTTON_WRITES):
        slave, register, value = BUTTON_WRITES[a['target']]
        rep.check('MODBUS trace', f"{a['target']} wrote {value} to slave {slave} register {register}", written(slave, register, value, a['epoch']))
    # writes the driver declared for its actions (touch panel): the value, and where stated, how many commands
    for a in (r for r in driver if r['action'] == 'expect_write'):
        slave, register, value, after, within = a['slave'], a['register'], a['value'], a['after'], a['within']
        what = f'touch panel wrote {value} to slave {slave} register {register}'
        rep.check('MODBUS trace', what, written(slave, register, value, after, within))
        if a.get('exactly') is not None:
            n = len([r for r in writes if r['slave'] == slave and r['register'] == register and after - 0.2 <= r['epoch'] <= after + within])
            rep.check('MODBUS trace', f"{what} as exactly {a['exactly']} command(s)", n == a['exactly'], f'{n} commands')
    for a in (r for r in driver if r['action'] == 'drag_end' and r['target'] in SLIDER_WRITES):
        slave, register, factor = SLIDER_WRITES[a['target']]
        rep.check('MODBUS trace', f"{a['target']} {a['value']} wrote {a['value'] * factor} to slave {slave} register {register}",
            written(slave, register, a['value'] * factor, a['epoch'] - 1.0, 3.0))

    # 3. the saved roast profile agrees with the wire and with the interaction log
    if alog_p:
        profile = ast.literal_eval(alog_p.read_text(encoding='utf-8'))
        start = float(profile['roastepoch'])
        timex, bt = profile['timex'], profile['temp2']
        stats['alog'] = {'file': alog_p.name, 'samples': len(timex), 'samplinginterval': profile.get('samplinginterval'),
            'timeindex': profile['timeindex'], 'events': len(profile.get('specialevents', []))}
        auto_marks = {r['mark'] for r in driver if r['action'] == 'mark_seen'}
        taps = {r['target']: r['epoch'] for r in driver if r['action'] == 'tap'}
        sim = {r['command']: r['epoch'] for r in driver if r['action'] == 'sim'}
        for i, m in enumerate(MARKS):
            idx = profile['timeindex'][i]
            if not ((i == 0 and idx > -1) or (i > 0 and idx > 0)):
                continue
            t_alog = start + timex[idx]
            if m in auto_marks: # auto-detected marks are back-dated to where the BT break started: compare with the physical event
                physical = sim.get(m.lower())
                if physical:
                    rep.check('roast profile', f'auto {m} placed at the physical {m.lower()}', abs(t_alog - physical) < 2.5,
                        f'{t_alog - physical:+.1f} s from the simulated {m.lower()}; detected {marks[m] - physical:+.1f} s after it' if m in marks else '')
            elif m in taps:
                lo, hi = MANUAL_MARK_WINDOW
                rep.check('roast profile', f'{m} time matches its tap (sample grid)', lo <= t_alog - taps[m] <= hi, f'{t_alog - taps[m]:+.1f} s')
        # BT values in the profile are values that were actually received from slave 25 (MODBUS input 1)
        bt_rx = [(r['epoch'], r['registers'][0]) for r in rx if r.get('slave') == 25 and r.get('fc') == 4 and r.get('registers')]
        mismatched = 0
        for t, v in zip(timex, bt, strict=False):
            if v == -1: # a lost reading, checked below
                continue
            near = [val for e, val in bt_rx if abs(e - (start + t)) < 2.5]
            if near and all(abs(v - n) > 1.01 for n in near):
                mismatched += 1
        # lost readings (-1) only on the channels of a slave whose requests were dropped; every other channel stays complete
        channels = {'BT': (profile['temp2'], 25), 'ET': (profile['temp1'], 21)}
        extra_slaves = [(24, 21), (22, 23)] # per extra device: (slave of curve 1, slave of curve 2)
        for i, (s1, s2) in enumerate(extra_slaves):
            if i < len(profile.get('extratemp1', [])):
                channels[f'extra {i+1}a'] = (profile['extratemp1'][i], s1)
                channels[f'extra {i+1}b'] = (profile['extratemp2'][i], s2)
        lost = {name: sum(1 for v in values if v == -1) for name, (values, _) in channels.items()}
        stats['lost_readings'] = lost
        others_clean = all(n == 0 for name, n in lost.items() if channels[name][1] not in dropped_slaves)
        rep.check('roast profile', 'no lost readings on channels without injected faults', others_clean, str(lost))
        if dropped_slaves:
            rep.check('roast profile', 'lost readings only where replies were dropped',
                any(n > 0 for name, n in lost.items() if channels[name][1] in dropped_slaves), str(lost))
        rep.check('roast profile', 'BT curve matches the values on the wire', mismatched <= 2, f'{mismatched} of {len(bt)} samples without a matching reading')
        strings = profile.get('specialeventsStrings', [])
        slider_events = len([s for s in strings if s])
        rep.check('roast profile', 'slider moves recorded as events', slider_events > 0, f'{len(profile.get("specialevents", []))} events')
        # quantified events (Q..) from the drive readbacks settle at the value the slider was set to
        events = [(profile['specialevents'][n], profile['specialeventstype'][n], round((profile['specialeventsvalue'][n] - 1) * 10), strings[n] if n < len(strings) else '')
            for n in range(len(profile.get('specialevents', [])))]
        etypes = profile.get('etypes', ['Air', 'Drum', 'Damper', 'Burner'])
        for etype in (0, 1):
            moves = [e for e in events if e[1] == etype and e[3] and not e[3].startswith('Q')]
            for k, move in enumerate(moves):
                if (23 if etype == 0 else 22, 8193, move[2] * 10) in rejected:
                    continue # the drive refused this value, so the readback is not expected to follow it
                nxt = moves[k + 1][0] if k + 1 < len(moves) else float('inf')
                qs = [e for e in events if e[1] == etype and e[3].startswith('Q') and move[0] <= e[0] < nxt]
                if qs:
                    rep.check('quantifiers', f'{etypes[etype]} readback settles at the slider value {move[2]}', abs(qs[-1][2] - move[2]) <= 1,
                        f'last quantified event {qs[-1][3]}')
        # roast assist: setup and weigh-out end up in the saved roast, heating elements are logged as events (type 2)
        if any(r['action'] == 'set_bean' for r in driver):
            weight = profile.get('weight', [0, 0, 'g'])
            rep.check('roast assist', 'bean and green weight from Roast setup saved', profile.get('beans') == 'India Natural' and float(weight[0]) == 1200.0,
                f"{profile.get('beans')!r}, {weight}")
            rep.check('roast assist', 'weight out from Weigh out saved (reading - container)', float(weight[1]) == 1024.0, str(weight))
            element_events = [(round(timex[profile['specialevents'][n]] - (timex[profile['timeindex'][0]] if profile['timeindex'][0] > -1 else 0)),
                strings[n]) for n in range(len(profile.get('specialevents', []))) if profile['specialeventstype'][n] == 2]
            stats['element_events'] = element_events
            rep.check('roast assist', 'element state recorded at CHARGE', any('at charge' in s and 'R' not in s.split('elements')[-1] for _, s in element_events),
                str(element_events))
            rep.check('roast assist', 'element toggle during the roast recorded', any(s.startswith('Right on') for _, s in element_events), str(element_events))
    # Claude's view through the MCP server (dev/mcp_probe.py ran mid-roast)
    probe_p = folder / 'mcp-probe.json'
    if probe_p.exists():
        probe = json.loads(probe_p.read_text(encoding='utf-8'))
        expected_tools = {'get_live_roast', 'get_roast_curve', 'list_roasts', 'get_roast', 'compare_with_history', 'add_roast_note'}
        rep.check('MCP server', 'all tools offered', expected_tools <= set(probe.get('tools', [])), str(probe.get('tools')))
        rep.check('MCP server', 'tool calls without errors', not probe.get('errors'), str(probe.get('errors')))
        live = probe.get('get_live_roast') or {}
        rep.check('MCP server', 'live roast seen while recording', isinstance(live, dict) and live.get('state') == 'recording'
            and isinstance(live.get('BT'), (int, float)) and 'CHARGE' in (live.get('marks') or {}),
            f"state {live.get('state') if isinstance(live, dict) else live}, BT {live.get('BT') if isinstance(live, dict) else ''}")
        rep.check('MCP server', 'roast history available', bool(probe.get('list_roasts')), f"{len(probe.get('list_roasts') or [])} roasts")
        curve = probe.get('get_roast_curve') or {}
        rep.check('MCP server', 'live curve available', bool(isinstance(curve, dict) and curve.get('rows')), f"{len(curve.get('rows', [])) if isinstance(curve, dict) else 0} rows")
        if alog_p:
            notes = str(ast.literal_eval(alog_p.read_text(encoding='utf-8')).get('roastingnotes', ''))
            rep.check('MCP server', 'note added through MCP saved with the roast', 'probe note from e2e' in notes, notes[-120:])
        expected = {'list_tastings', 'add_tasting'}
        rep.check('MCP server', 'tasting tools offered', expected <= set(probe.get('tools', [])), str(probe.get('tools')))
        added = probe.get('add_tasting') or {}
        rep.check('MCP server', 'tasting added through MCP', isinstance(added, dict) and added.get('source') == 'claude'
            and added.get('development') == 'under' and added.get('method') == 'Pour-over', str(added)[:200])

    # tasting log (written by the touch dialog and by Claude through MCP), in the test folder only
    tasting_p = folder / 'tasting-log.jsonl'
    tastings = load_jsonl(tasting_p) if tasting_p.exists() else []
    stats['tastings'] = len(tastings)
    rep.check('tasting log', 'written to the test folder', bool(tastings), f'{len(tastings)} entries')
    rep.check('tasting log', 'entry from the touch dialog', any(t.get('method') == 'Espresso' and t.get('grind') == 12.5 for t in tastings))
    rep.check('tasting log', 'entry from Claude (MCP)', any(t.get('notes') == 'probe tasting from e2e' for t in tastings))

    # Claude panel, against dev/fake_claude_api.py
    requests_p = folder / 'claude-requests.jsonl'
    requests = [r for r in load_jsonl(requests_p) if r.get('path', '').startswith('/v1/messages')] if requests_p.exists() else []
    stats['claude_requests'] = len(requests)
    rep.check('Claude panel', 'two questions with a tool round each (4 requests)', len(requests) == 4, f'{len(requests)} requests')
    if requests:
        first = requests[0]
        body, headers = first['body'], first['headers']
        rep.check('Claude panel', 'model claude-opus-5-5', body.get('model') == 'claude-opus-5-5', str(body.get('model')))
        rep.check('Claude panel', 'adaptive thinking and explicit effort', (body.get('thinking') or {}).get('type') == 'adaptive'
            and (body.get('output_config') or {}).get('effort') == 'medium', f"{body.get('thinking')} {body.get('output_config')}")
        rep.check('Claude panel', 'server-side fallbacks (default) with their beta header', body.get('fallbacks') == 'default'
            and 'server-side-fallback-2026-07-01' in headers.get('anthropic-beta', ''), f"{body.get('fallbacks')} / {headers.get('anthropic-beta')}")
        tools = sorted(t.get('name', '') for t in body.get('tools', []))
        rep.check('Claude panel', 'the read-only roast tools are offered (8)', len(tools) == 8 and 'get_live_roast' in tools and 'add_tasting' in tools, str(tools))
        rep.check('Claude panel', 'no machine-control tool offered', not any(w in ' '.join(tools) for w in ('set_', 'write', 'fan', 'drum', 'heat', 'mark')), str(tools))
        # the SDK sends the key as x-api-key or (2.x) as a bearer token
        rep.check('Claude panel', 'API key taken from the environment (ANTHROPIC_API_KEY)',
            'e2e-test-key' in (headers.get('x-api-key', ''), headers.get('authorization', '').removeprefix('Bearer ')))
    if len(requests) >= 2:
        msgs = requests[1]['body'].get('messages', [])
        results = [b for m in msgs if isinstance(m.get('content'), list) for b in m['content'] if b.get('type') == 'tool_result']
        rep.check('Claude panel', 'both tool calls answered in one turn', len(results) == 2 and all(not r.get('is_error') for r in results),
            str([str(r.get('content'))[:80] for r in results]))
    if len(requests) >= 3:
        msgs = requests[2]['body'].get('messages', [])
        roles = [m.get('role') for m in msgs]
        rep.check('Claude panel', 'follow-up carries the whole conversation', roles == ['user', 'assistant', 'user', 'assistant', 'user'], str(roles))
    replies = [r for r in driver if r['action'] == 'claude_reply']
    rep.check('Claude panel', 'replies shown in the panel', len(replies) == 2, str([r.get('text') for r in replies]))

    # roast archive: the test roast and the tasting log committed and pushed to the throwaway remote
    archive, remote = folder / 'archive', folder / 'archive-remote.git'
    if archive.exists():
        git = find_tool('git') or 'git'
        def git_out(*args: str, repo: Path = archive) -> str:
            return subprocess.run([git, '-C', str(repo), *args], capture_output=True, text=True, check=False).stdout
        files = set(git_out('ls-tree', '-r', '--name-only', 'HEAD', repo=remote).splitlines())
        rep.check('roast archive', 'test roast committed and pushed', bool(alog_p) and f'roasts/{alog_p.name}' in files,
            f'{len(files)} files in the remote')
        rep.check('roast archive', 'tasting log committed and pushed', 'tasting-log.jsonl' in files)
        rep.check('roast archive', 'roast history archived', sum(1 for f in files if f.startswith('roasts/')) > 1,
            f"{sum(1 for f in files if f.startswith('roasts/'))} roasts")
        stats['archive_commits'] = git_out('log', '--oneline', repo=remote).splitlines()[:5]
        rep.check('roast archive', 'nothing left uncommitted', not git_out('status', '--porcelain').strip(), git_out('status', '--porcelain')[:200])

    if stats.get('speed', 0) and stats['speed'] <= 1:
        rep.check('driver', 'DROP auto-detected (real-time roast)', stats['marks_auto'].get('DROP', False))

    # 4. screen recording covers the session
    if session_p:
        meta = json.loads(session_p.read_text(encoding='utf-8-sig'))
        video = session_p.parent / meta['video']
        duration = None
        ffprobe = find_tool('ffprobe')
        if video.exists() and ffprobe:
            out = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(video)], capture_output=True, text=True, check=False)
            try:
                duration = float(out.stdout.strip())
            except ValueError:
                duration = None
        rep.check('screen recording', 'recording started before the first action', meta['start_epoch'] <= t0, f"{t0 - meta['start_epoch']:.1f} s before")
        rep.check('screen recording', 'recording covers the whole session', duration is not None and meta['start_epoch'] + duration >= t_end,
            f'{duration:.0f} s of video' if duration else 'duration unknown')

    finish(folder, rep, stats)


def finish(folder: Path, rep: Report, stats: dict[str, Any]) -> None:
    (folder / 'e2e-report.json').write_text(json.dumps({'checks': rep.checks, 'stats': stats}, indent=1), encoding='utf-8')
    lines = [f'# End-to-end capture test: {len(rep.checks) - len(rep.failed)}/{len(rep.checks)} checks passed', '']
    area = None
    for c in rep.checks:
        if c['area'] != area:
            area = c['area']
            lines += ['', f'## {area}', '']
        lines.append(f"- {'PASS' if c['ok'] else '**FAIL**'} {c['check']}" + (f" ({c['detail']})" if c['detail'] else ''))
    lines += ['', '## stats', '', '```', json.dumps(stats, indent=1), '```']
    (folder / 'e2e-report.md').write_text('\n'.join(lines), encoding='utf-8')
    print('\n'.join(lines[:3]))
    for c in rep.checks:
        print(('PASS ' if c['ok'] else 'FAIL ') + c['area'] + ': ' + c['check'] + (f" ({c['detail']})" if c['detail'] else ''))
    sys.exit(1 if rep.failed else 0)


if __name__ == '__main__':
    main()
