#
# ABOUT
# The roast tools Claude uses, shared by the MCP server (artisanlib/mcp_server.py) and the in-app Claude panel
# (artisanlib/claude_panel.py): the live roast, its curve, past roasts, the comparison with matched roasts, the
# tasting log, and notes.
#
# Read-only towards the machine: nothing here changes fan, drum, heat, marks or anything sent over MODBUS. The writes
# are a note on the current roast and a tasting entry. Called from worker threads; the note goes to the GUI thread.

# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import re
import ast
import logging
import datetime
from typing import Final, Any, TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal

from artisanlib.element_lanes import element_periods, summary as element_summary
from artisanlib.roast_history import normalize_bean
from artisanlib.tasting_log import METHODS, VERDICTS, DESCRIPTORS, roast_key, days_off_roast

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import
    from artisanlib.roast_assist import RoastAssist # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

MARKS: Final[list[str]] = ['CHARGE', 'DRY END', 'FC START', 'FC END', 'SC START', 'SC END', 'DROP', 'COOL END']

INSTRUCTIONS: Final[str] = """\
You are connected to Artisan, the roast logging software of a Coffee-Tech FZ94 drum roaster (electric elements
left/center/right switched by hand; an ET controller cuts the heat at its heat limit; fan and drum speeds are set by the
operator). Temperatures are in degrees Celsius, times in seconds after CHARGE unless stated. The elements are either on
or off (no intensity); heating_elements has when each was on and its total on time (all three are on at CHARGE unless
the roast says otherwise). Fan and drum speeds are drive frequencies in Hz.

Use get_live_roast to see the roast in progress (refresh it before each answer during a roast: it changes every 2 s),
get_roast_curve for its curve, compare_with_history to put it against matched past roasts, and list_roasts / get_roast
for history. Compare roasts of the same bean and a similar batch size (green weight within 10%): batch size changes the
timing a lot. list_tastings has how the roasts tasted and how they were brewed (method, grinder, grind setting, dose,
yield, time): use it to connect roast decisions to the cup, eg. a roast that needs a much finer grind than others of
the same bean and method to extract may be under-developed.

You cannot control the roaster. Advise the operator, who decides. Be concise during a roast: the operator is at the
machine. Use add_roast_note to keep what the operator tells you (decisions, sensory cues) with the roast, and
add_tasting for a tasting result."""


def mmss(seconds:float|None) -> str|None:
    if seconds is None:
        return None
    s = max(0, int(round(seconds)))
    return f'{s // 60}:{s % 60:02d}'


def public(r:dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if not k.startswith('_') and k != 'path'}


class NoteRelay(QObject): # pyright: ignore[reportGeneralTypeIssues]
    """Carries notes from a worker thread to the GUI thread."""
    note = pyqtSignal(str)


class RoastTools:
    def __init__(self, aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
        self.aw = aw
        self.assist = assist
        self.relay = NoteRelay()
        self.relay.note.connect(self.append_note)

    # ---- the roast in progress (read from worker threads; the lists are only appended to while sampling)

    def live(self) -> dict[str, Any]:
        aw, qmc = self.aw, self.aw.qmc
        state = 'recording' if qmc.flagstart else ('monitoring' if qmc.flagon else 'off')
        def last(values:list[Any]) -> float|None:
            for v in reversed(list(values[-5:])):
                if isinstance(v, (int, float)) and v != -1:
                    return round(float(v), 1)
            return None
        temps = (qmc.temp1, qmc.temp2, qmc.delta2) if qmc.flagstart else (qmc.on_temp1, qmc.on_temp2, getattr(qmc, 'on_delta2', []))
        extras:dict[str, float|None] = {}
        try:
            for i in range(len(qmc.extradevices)):
                for names, values in ((qmc.extraname1, qmc.extratemp1 if qmc.flagstart else qmc.on_extratemp1),
                        (qmc.extraname2, qmc.extratemp2 if qmc.flagstart else qmc.on_extratemp2)):
                    if i < len(names) and i < len(values):
                        name = names[i].format(*qmc.etypes) if '{' in names[i] else names[i]
                        extras[name] = last(values[i])
        except Exception as e: # pylint: disable=broad-except
            _log.debug(e)
        marks:dict[str, Any] = {}
        charge = None
        try:
            if qmc.timeindex[0] > -1 and qmc.timex:
                charge = qmc.timex[qmc.timeindex[0]]
                for i, name in enumerate(MARKS):
                    idx = qmc.timeindex[i]
                    if (i == 0 and idx > -1) or (i > 0 and idx > 0):
                        marks[name] = {'time': round(qmc.timex[idx] - charge, 1), 'clock': mmss(qmc.timex[idx] - charge),
                            'BT': qmc.temp2[idx], 'ET': qmc.temp1[idx]}
        except Exception as e: # pylint: disable=broad-except
            _log.debug(e)
        roast_time = None
        if charge is not None:
            end = qmc.timex[qmc.timeindex[6]] if qmc.timeindex[6] > 0 else qmc.timeclock.elapsedMilli()
            roast_time = round(end - charge, 1)
        dev = None
        if 'FC START' in marks and roast_time:
            dev_s = roast_time - marks['FC START']['time']
            dev = {'time': round(dev_s, 1), 'clock': mmss(dev_s), 'ratio_pct': round(100 * dev_s / roast_time, 1)}
        sliders = {}
        for n in range(4):
            try:
                if aw.eventslidervisibilities[n]:
                    sliders[qmc.etypes[n]] = aw.eventslidervalues[n]
            except Exception: # pylint: disable=broad-except
                pass
        events = []
        try:
            for n in range(max(0, len(qmc.specialevents) - 20), len(qmc.specialevents)):
                idx = qmc.specialevents[n]
                t = qmc.timex[idx] - charge if charge is not None else qmc.timex[idx]
                etype = qmc.specialeventstype[n]
                events.append({'time': round(t, 1), 'clock': mmss(t), 'type': qmc.etypes[etype] if etype < len(qmc.etypes) else str(etype),
                    'value': round((qmc.specialeventsvalue[n] - 1) * 10), 'label': qmc.specialeventsStrings[n]})
        except Exception as e: # pylint: disable=broad-except
            _log.debug(e)
        weight = qmc.weight
        guide = ''
        elements = None
        if self.assist is not None:
            guide = ' '.join(re.sub(r'<[^>]+>', '', self.assist.guide.text().replace('<br>', ' — ')).split())
            elements = dict(zip(['left', 'center', 'right'], self.assist.elements, strict=True))
        element_times = None
        try:
            if charge is not None:
                element_times = element_summary(element_periods(qmc.timex, qmc.specialevents, qmc.specialeventstype,
                    qmc.specialeventsStrings), charge, charge + roast_time if roast_time is not None else None)
        except Exception as e: # pylint: disable=broad-except
            _log.debug(e)
        bg = str(getattr(qmc, 'backgroundpath', '') or '') if getattr(qmc, 'background', False) else ''
        return {
            'state': state, 'sampling_interval_s': qmc.delay / 1000,
            'bean': qmc.beans, 'roast_name': qmc.title, 'batch': getattr(qmc, 'roastbatchnr', None),
            'weight_in': weight[0], 'weight_out': weight[1], 'weight_unit': weight[2],
            'roast_time': roast_time, 'roast_clock': mmss(roast_time),
            'BT': last(temps[1]), 'ET': last(temps[0]), 'BT_rate_of_rise_per_min': last(temps[2]),
            'extra_channels': extras, 'controls': sliders, 'heating_elements_on': elements, 'heating_elements': element_times,
            'marks': marks, 'development': dev, 'background_roast': os.path.basename(bg) if bg else None,
            'recent_events': events, 'guide': guide, 'roasting_notes': qmc.roastingnotes,
        }

    def curve(self, step_s:float) -> dict[str, Any]:
        qmc = self.aw.qmc
        step_s = max(2.0, step_s)
        timex, bt, et = list(qmc.timex), list(qmc.temp2), list(qmc.temp1)
        ror = list(qmc.delta2)
        charge = timex[qmc.timeindex[0]] if qmc.timeindex[0] > -1 and timex else (timex[0] if timex else 0)
        rows = []
        next_t = -1e9
        for i, t in enumerate(timex):
            if t - next_t >= step_s:
                next_t = t
                r = ror[i] if i < len(ror) else None
                rows.append([round(t - charge, 1), bt[i] if i < len(bt) else None, et[i] if i < len(et) else None,
                    round(r, 1) if isinstance(r, (int, float)) else None])
        return {'columns': ['time_s_after_charge', 'BT', 'ET', 'BT_RoR_per_min'], 'rows': rows}

    # ---- history

    def tasting_summary(self, name:str) -> list[dict[str, Any]]:
        if self.assist is None:
            return []
        return [{k: e.get(k) for k in ('method', 'grinder', 'grind', 'dose_g', 'yield_g', 'time_s', 'score', 'development',
            'descriptors', 'notes', 'days_off_roast') if e.get(k) not in (None, '', [])} for e in self.assist.tastings.for_roast(name)]

    def list_roasts(self, bean:str = '', min_weight_g:float = 0.0, max_weight_g:float = 0.0, limit:int = 20) -> list[dict[str, Any]]:
        if self.assist is None:
            return []
        rows = []
        for r in self.assist.history.roasts():
            if bean and normalize_bean(bean) not in normalize_bean(r['bean']):
                continue
            w = r.get('weight_in_g') or 0
            if (min_weight_g and w < min_weight_g) or (max_weight_g and w > max_weight_g):
                continue
            row = public(r)
            tastings = self.tasting_summary(r['name'])
            if tastings:
                row['tastings'] = tastings
            rows.append(row)
            if len(rows) >= max(1, min(limit, 200)):
                break
        return rows

    def get_roast(self, name:str, step_s:float = 10.0) -> dict[str, Any]:
        if self.assist is None:
            return {'error': 'no roast history'}
        for r in self.assist.history.roasts():
            if r['name'] == name or roast_key(r['name']) == roast_key(name):
                try:
                    with open(r['path'], encoding='utf-8') as f:
                        p = ast.literal_eval(f.read())
                    timex, ti = p.get('timex', []), p.get('timeindex', [-1])
                    charge = timex[ti[0]] if ti and ti[0] > -1 and timex else 0
                    rows, next_t = [], -1e9
                    for i, t in enumerate(timex):
                        if t - next_t >= max(2.0, step_s):
                            next_t = t
                            rows.append([round(t - charge, 1), p['temp2'][i], p['temp1'][i]])
                    etypes = p.get('etypes', ['Air', 'Drum', 'Damper', 'Burner'])
                    events = [{'time': round(timex[i] - charge, 1), 'type': etypes[p['specialeventstype'][n]] if p['specialeventstype'][n] < len(etypes) else '',
                        'value': round((p['specialeventsvalue'][n] - 1) * 10), 'label': p['specialeventsStrings'][n]}
                        for n, i in enumerate(p.get('specialevents', []))]
                    drop = timex[ti[6]] if len(ti) > 6 and ti[6] > 0 else (timex[-1] if timex else None)
                    elements = element_summary(element_periods(timex, p.get('specialevents', []), p.get('specialeventstype', []),
                        p.get('specialeventsStrings', [])), charge, drop)
                    return {'summary': public(r), 'events': events, 'heating_elements': elements if any(
                        e['on_periods'] for e in elements.values()) else 'not logged in this roast',
                        'tastings': self.tasting_summary(r['name']),
                        'curve': {'columns': ['time_s_after_charge', 'BT', 'ET'], 'rows': rows}}
                except Exception as e: # pylint: disable=broad-except
                    return {'error': str(e)}
        return {'error': f'no roast named {name!r}'}

    def compare_with_history(self) -> dict[str, Any]:
        live = self.live()
        if self.assist is None:
            return {'current': live}
        w = self.assist.weight_in_g()
        bg = self.assist.background_entry()
        return {
            'current': {k: live[k] for k in ('bean', 'weight_in', 'roast_time', 'marks', 'development')},
            'background': public(bg) if bg else None,
            'same_batch_size_medians': self.assist.history.batch_size_medians(w) if w > 0 else None,
            'matched_same_bean_and_size': [public(r) for r in self.assist.history.similar(str(self.aw.qmc.beans or ''), w or None)[:5]],
        }

    # ---- tastings

    def list_tastings(self, roast:str = '', bean:str = '', method:str = '', limit:int = 30) -> list[dict[str, Any]]:
        if self.assist is None:
            return []
        return self.assist.tastings.query(roast=roast, bean=bean, method=method, limit=max(1, min(limit, 500)))

    def add_tasting(self, roast:str, method:str, score:int = 0, development:str = '', grinder:str = '', grind:str = '',
            dose_g:float = 0.0, yield_g:float = 0.0, time_s:float = 0.0, descriptors:list[str]|None = None, notes:str = '') -> dict[str, Any]:
        if self.assist is None:
            return {'error': 'no roast history'}
        match = next((r for r in self.assist.history.roasts() if r['name'] == roast or roast_key(r['name']) == roast_key(roast)), None)
        if match is None:
            return {'error': f'no roast named {roast!r}; use list_roasts'}
        if development and development not in {k for k, _ in VERDICTS}:
            return {'error': f"development must be one of {[k for k, _ in VERDICTS]}"}
        method = next((m for m in METHODS if m.lower() == method.strip().lower()), method.strip())
        g:float|str = grind
        try:
            g = float(grind)
        except (TypeError, ValueError):
            pass
        entry = self.assist.tastings.add({
            'roast': match['name'], 'bean': match.get('bean', ''), 'roast_date': match.get('date', ''),
            'days_off_roast': days_off_roast(match.get('date', ''), datetime.datetime.now().astimezone().date()),
            'method': method, 'grinder': grinder.strip(), 'grind': g,
            'dose_g': dose_g or None, 'yield_g': yield_g or None, 'time_s': time_s or None,
            'score': score if 1 <= score <= 5 else None, 'development': development,
            'descriptors': [d for d in (descriptors or []) if d], 'notes': ' '.join(notes.split())[:500], 'source': 'claude',
        })
        self.assist.relay_tasting(entry)
        return entry

    # ---- notes (written on the GUI thread)

    def add_roast_note(self, text:str) -> str:
        text = ' '.join(str(text).split())[:500]
        if not text:
            return 'empty note ignored'
        self.relay.note.emit(text)
        return 'note added'

    def append_note(self, text:str) -> None:
        qmc = self.aw.qmc
        stamp = datetime.datetime.now(datetime.UTC).astimezone().strftime('%H:%M') # local wall clock for the operator
        roast = ''
        try:
            if qmc.flagstart and qmc.timeindex[0] > -1:
                roast = f' (roast {mmss(qmc.timeclock.elapsedMilli() - qmc.timex[qmc.timeindex[0]])})'
        except Exception: # pylint: disable=broad-except
            pass
        qmc.roastingnotes = (qmc.roastingnotes + '\n' if qmc.roastingnotes else '') + f'[{stamp}{roast}] {text}'
        qmc.fileDirtySignal.emit()


# tool descriptions shared by the MCP server and the Claude panel
DESCRIPTIONS: Final[dict[str, str]] = {
    'get_live_roast': 'The roast in progress (or the last one): state, temperatures, rate of rise, fan/drum settings, '
        'heating elements, marks, development time and ratio, events, the on-screen guide and notes.',
    'get_roast_curve': 'The current roast curve since CHARGE, one row every step_s seconds: time, BT, ET, BT rate of rise.',
    'list_roasts': 'Past roasts, newest first, optionally filtered by bean (case-insensitive) and green weight range in '
        'grams. Each with weights, loss, times/temperatures of DRY END, first crack and DROP, and its tastings.',
    'get_roast': "One past roast by its name or batch number (eg. '160' or '160 India Natural ...'): summary, events, "
        'tastings and the curve every step_s seconds.',
    'compare_with_history': 'The current roast against the loaded background roast and the medians of past roasts of '
        'the same batch size: phase times, first crack and development.',
    'list_tastings': 'Tastings, newest first, optionally for one roast (name or batch number), bean or brew method: '
        'method, grinder, grind setting, dose, yield, time, score 1-5, development verdict, descriptors, notes.',
    'add_tasting': 'Log how a roast tasted, as told by the operator. development: under, right or over. method: one of '
        + ', '.join(METHODS) + '. descriptors: any of ' + ', '.join(DESCRIPTORS) + '.',
    'add_roast_note': 'Add a note to the current roast (saved with it), eg. a decision or a sensory cue told by the '
        'operator. Prefixed with the time.',
}
