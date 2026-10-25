#
# ABOUT
# Optional MCP server that lets Claude (Claude Code or the Claude desktop app, with the user's own Claude login)
# follow the live roast and look up past roasts, to guide and teach while roasting.
#
# It is read-only towards the machine: no tool changes fan, drum, heat, marks or anything sent over MODBUS.
# The only write is adding a note to the current roast's roasting notes.
#
# Runs on its own thread with streamable HTTP, bound to 127.0.0.1 only (default port 8765, path /mcp).
# Enabled with the environment variable ARTISAN_MCP=1 or View > Claude Assistant (MCP server).
# Requires the 'mcp' package (2.x); without it the menu entry says so and nothing else changes.

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
import asyncio
import logging
import threading
import datetime
from typing import Final, Any, TYPE_CHECKING

from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import
    from artisanlib.roast_assist import RoastAssist # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_PORT: Final[int] = 8765
MARKS: Final[list[str]] = ['CHARGE', 'DRY END', 'FC START', 'FC END', 'SC START', 'SC END', 'DROP', 'COOL END']

INSTRUCTIONS: Final[str] = """\
You are connected to Artisan, the roast logging software of a Coffee-Tech FZ94 drum roaster (electric elements
left/center/right switched by hand; an ET controller cuts the heat at its heat limit; fan and drum speeds are set by the
operator). Temperatures are in degrees Celsius, times in seconds after CHARGE unless stated.

Use get_live_roast to see the roast in progress (refresh it before each answer during a roast: it changes every 2 s),
get_roast_curve for its curve, compare_with_history to put it against matched past roasts, and list_roasts / get_roast
for history. Compare roasts of the same bean and a similar batch size (green weight within 10%): batch size changes the
timing a lot.

You cannot control the roaster. Advise the operator, who decides. Be concise during a roast: the operator is at the
machine. Use add_roast_note to keep what the operator tells you (decisions, sensory cues, tasting results) with the
roast."""


def mmss(seconds:float|None) -> str|None:
    if seconds is None:
        return None
    s = max(0, int(round(seconds)))
    return f'{s // 60}:{s % 60:02d}'


class NoteRelay(QObject): # pyright: ignore[reportGeneralTypeIssues]
    """Carries notes from the MCP thread to the GUI thread."""
    note = pyqtSignal(str)


class RoastMcpServer:
    def __init__(self, aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
        self.aw = aw
        self.assist = assist
        self.port = int(QSettings().value('Assistant/mcpPort', DEFAULT_PORT, type=int))
        self.thread:threading.Thread|None = None
        self.error:str = ''
        self.relay = NoteRelay()
        self.relay.note.connect(self.append_note)

    # ---- snapshots of the app state (read from the MCP thread; the lists are only appended to while sampling)

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
        bg = str(getattr(qmc, 'backgroundpath', '') or '') if getattr(qmc, 'background', False) else ''
        return {
            'state': state, 'sampling_interval_s': qmc.delay / 1000,
            'bean': qmc.beans, 'roast_name': qmc.title, 'batch': getattr(qmc, 'roastbatchnr', None),
            'weight_in': weight[0], 'weight_out': weight[1], 'weight_unit': weight[2],
            'roast_time': roast_time, 'roast_clock': mmss(roast_time),
            'BT': last(temps[1]), 'ET': last(temps[0]), 'BT_rate_of_rise_per_min': last(temps[2]),
            'extra_channels': extras, 'controls': sliders, 'heating_elements_on': elements,
            'marks': marks, 'development': dev, 'background_roast': os.path.basename(bg) if bg else None,
            'recent_events': events, 'guide': guide, 'roasting_notes': qmc.roastingnotes,
        }

    def curve(self, step_s:float) -> dict[str, Any]:
        qmc = self.aw.qmc
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

    # ---- notes (written on the GUI thread)

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

    # ---- server

    def build(self) -> Any:
        from mcp.server.mcpserver import MCPServer # type: ignore[import-not-found,unused-ignore] # pylint: disable=import-error
        server = MCPServer('artisan-roaster', instructions=INSTRUCTIONS)

        @server.tool(description='The roast in progress (or the last one): state, temperatures, rate of rise, fan/drum ' # type: ignore[untyped-decorator,unused-ignore]
            'settings, heating elements, marks, development time and ratio, events, the on-screen guide and notes.')
        def get_live_roast() -> dict[str, Any]:
            return self.live()

        @server.tool(description='The current roast curve since CHARGE, one row every step_s seconds: time, BT, ET, BT rate of rise.') # type: ignore[untyped-decorator,unused-ignore]
        def get_roast_curve(step_s:float = 10.0) -> dict[str, Any]:
            return self.curve(max(2.0, step_s))

        @server.tool(description='Past roasts, newest first, optionally filtered by bean (case-insensitive) and green ' # type: ignore[untyped-decorator,unused-ignore]
            'weight range in grams. Each with weights, loss, and times/temperatures of DRY END, first crack and DROP.')
        def list_roasts(bean:str = '', min_weight_g:float = 0.0, max_weight_g:float = 0.0, limit:int = 20) -> list[dict[str, Any]]:
            if self.assist is None:
                return []
            from artisanlib.roast_history import normalize_bean
            rows = []
            for r in self.assist.history.roasts():
                if bean and normalize_bean(bean) not in normalize_bean(r['bean']):
                    continue
                w = r.get('weight_in_g') or 0
                if (min_weight_g and w < min_weight_g) or (max_weight_g and w > max_weight_g):
                    continue
                rows.append({k: v for k, v in r.items() if not k.startswith('_') and k != 'path'})
                if len(rows) >= max(1, min(limit, 200)):
                    break
            return rows

        @server.tool(description="One past roast by its name or batch number (eg. '160' or '160 India Natural ...'): " # type: ignore[untyped-decorator,unused-ignore]
            'summary, events and the curve every step_s seconds.')
        def get_roast(name:str, step_s:float = 10.0) -> dict[str, Any]:
            if self.assist is None:
                return {'error': 'no roast history'}
            for r in self.assist.history.roasts():
                if r['name'] == name or r['name'].split(' ')[0] == name.strip():
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
                        summary = {k: v for k, v in r.items() if not k.startswith('_') and k != 'path'}
                        return {'summary': summary, 'events': events, 'curve': {'columns': ['time_s_after_charge', 'BT', 'ET'], 'rows': rows}}
                    except Exception as e: # pylint: disable=broad-except
                        return {'error': str(e)}
            return {'error': f'no roast named {name!r}'}

        @server.tool(description='The current roast against the loaded background roast and the medians of past roasts ' # type: ignore[untyped-decorator,unused-ignore]
            'of the same batch size: phase times, first crack and development.')
        def compare_with_history() -> dict[str, Any]:
            live = self.live()
            if self.assist is None:
                return {'current': live}
            w = self.assist.weight_in_g()
            bg = self.assist.background_entry()
            return {
                'current': {k: live[k] for k in ('bean', 'weight_in', 'roast_time', 'marks', 'development')},
                'background': {k: v for k, v in bg.items() if not k.startswith('_') and k != 'path'} if bg else None,
                'same_batch_size_medians': self.assist.history.batch_size_medians(w) if w > 0 else None,
                'matched_same_bean_and_size': [{k: v for k, v in r.items() if not k.startswith('_') and k != 'path'}
                    for r in self.assist.history.similar(str(self.aw.qmc.beans or ''), w or None)[:5]],
            }

        @server.tool(description='Add a note to the current roast (saved with it), eg. a decision, a sensory cue or a ' # type: ignore[untyped-decorator,unused-ignore]
            'tasting result told by the operator. Prefixed with the time.')
        def add_roast_note(text:str) -> str:
            text = ' '.join(str(text).split())[:500]
            if not text:
                return 'empty note ignored'
            self.relay.note.emit(text) # the GUI thread writes it
            return 'note added'

        return server

    def start(self) -> bool:
        if self.thread is not None and self.thread.is_alive():
            return True
        try:
            server = self.build()
        except ImportError:
            self.error = "the 'mcp' package is not installed (run dev\\setup-dev.ps1)"
            return False
        except Exception as e: # pylint: disable=broad-except
            self.error = str(e)
            _log.exception(e)
            return False
        def run() -> None:
            try:
                asyncio.run(server.run_streamable_http_async(host='127.0.0.1', port=self.port))
            except Exception as e: # pylint: disable=broad-except
                self.error = str(e)
                _log.exception(e)
        self.thread = threading.Thread(target=run, daemon=True, name='artisan-mcp')
        self.thread.start()
        _log.info('MCP server: http://127.0.0.1:%d/mcp', self.port)
        return True


_instance:RoastMcpServer|None = None


def install(aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
    global _instance # pylint: disable=global-statement
    if _instance is not None or aw.app.artisanviewerMode:
        return
    _instance = RoastMcpServer(aw, assist)
    server = _instance
    enabled = os.environ.get('ARTISAN_MCP', '').strip() == '1' or bool(QSettings().value('Assistant/mcpEnabled', False, type=bool))
    action = QAction(QApplication.translate('Menu', 'Claude Assistant (MCP server)'), aw)
    action.setCheckable(True)
    def toggled(on:bool) -> None:
        QSettings().setValue('Assistant/mcpEnabled', on)
        if on:
            if server.start():
                aw.sendmessage(f'Claude assistant: MCP server on http://127.0.0.1:{server.port}/mcp')
            else:
                action.setChecked(False)
                aw.sendmessage(f'Claude assistant not started: {server.error}')
        else:
            aw.sendmessage('Claude assistant: the MCP server stops when Artisan is restarted')
    action.toggled.connect(toggled)
    if aw.viewMenu is not None:
        aw.viewMenu.addAction(action)
    if enabled:
        action.setChecked(True) # starts the server
