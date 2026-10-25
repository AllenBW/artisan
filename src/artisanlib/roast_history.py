#
# ABOUT
# Index of the roasts saved in the profile folder: bean, weights, date and the times and temperatures of the
# phase marks, read once per file and cached (keyed by file size and modification time). Used to pick a matching
# background roast, to derive phase targets for the roast guide and to answer questions about past roasts.

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
import ast
import json
import logging
import threading
import statistics
from pathlib import Path
from typing import Final, Any

_log: Final[logging.Logger] = logging.getLogger(__name__)

CACHE_VERSION: Final[int] = 1
WEIGHT_TOLERANCE: Final[float] = 0.10 # a roast of a similar batch size is within +-10% of the green weight


def normalize_bean(name:str) -> str:
    return ' '.join(str(name or '').replace('\\n', ' ').split()).lower()


def summarize(profile:dict[str, Any], path:str) -> dict[str, Any]:
    computed = profile.get('computed', {}) or {}
    weight = profile.get('weight', [0, 0, 'g']) or [0, 0, 'g']
    unit = str(weight[2]) if len(weight) > 2 else 'g'
    factor = {'g': 1.0, 'kg': 1000.0, 'lb': 453.592, 'oz': 28.3495}.get(unit.lower(), 1.0)
    w_in = float(weight[0] or 0) * factor
    w_out = float(weight[1] or 0) * factor
    def t(key:str) -> float|None:
        v = computed.get(key)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None
    drop = t('DROP_time')
    fcs = t('FCs_time')
    return {
        'path': path,
        'name': Path(path).stem,
        'bean': ' '.join(str(profile.get('beans', '')).replace('\\n', ' ').split()),
        'title': str(profile.get('title', '')),
        'batch': profile.get('roastbatchnr'),
        'date': str(profile.get('roastisodate', '')),
        'time': str(profile.get('roasttime', '')),
        'epoch': profile.get('roastepoch'),
        'weight_in_g': round(w_in, 1),
        'weight_out_g': round(w_out, 1),
        'loss_pct': round(100 * (w_in - w_out) / w_in, 1) if w_in > 0 and w_out > 0 else None,
        'charge_bt': computed.get('CHARGE_BT'), 'charge_et': computed.get('CHARGE_ET'),
        'tp_time': t('TP_time'), 'tp_bt': computed.get('TP_BT'),
        'dry_time': t('DRY_time'), 'dry_bt': computed.get('DRY_BT'),
        'fcs_time': fcs, 'fcs_bt': computed.get('FCs_BT'),
        'drop_time': drop, 'drop_bt': computed.get('DROP_BT'), 'drop_et': computed.get('DROP_ET'),
        'dev_time': (drop - fcs) if drop and fcs else None,
        'dtr_pct': round(100 * (drop - fcs) / drop, 1) if drop and fcs else None,
        'notes': str(profile.get('roastingnotes', '') or ''),
        'cupping_notes': str(profile.get('cuppingnotes', '') or ''),
    }


class RoastHistory:
    """Thread-safe index of the .alog files in a folder."""

    def __init__(self, folder:str, cache_file:str|None) -> None:
        self.folder = folder
        self.cache_file = cache_file
        self.lock = threading.Lock()
        self.entries:dict[str, dict[str, Any]] = {} # path -> summary (with '_key': [size, mtime])

    def load_cache(self) -> None:
        if self.cache_file and os.path.isfile(self.cache_file):
            try:
                with open(self.cache_file, encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('version') == CACHE_VERSION:
                    self.entries = data.get('entries', {})
            except Exception as e: # pylint: disable=broad-except
                _log.debug(e)

    def save_cache(self) -> None:
        if self.cache_file:
            try:
                Path(self.cache_file).parent.mkdir(parents=True, exist_ok=True)
                with open(self.cache_file, 'w', encoding='utf-8') as f:
                    json.dump({'version': CACHE_VERSION, 'entries': self.entries}, f)
            except Exception as e: # pylint: disable=broad-except
                _log.debug(e)

    # (re)reads new or changed files; returns the number of files read
    def refresh(self) -> int:
        read = 0
        try:
            files = [str(p) for p in Path(self.folder).glob('*.alog')] if self.folder and os.path.isdir(self.folder) else []
        except Exception as e: # pylint: disable=broad-except
            _log.debug(e)
            return 0
        entries:dict[str, dict[str, Any]] = {}
        for path in files:
            try:
                st = os.stat(path)
                key = [st.st_size, int(st.st_mtime)]
                old = self.entries.get(path)
                if old is not None and old.get('_key') == key:
                    entries[path] = old
                    continue
                with open(path, encoding='utf-8') as f:
                    profile = ast.literal_eval(f.read())
                summary = summarize(profile, path)
                summary['_key'] = key
                entries[path] = summary
                read += 1
            except Exception as e: # pylint: disable=broad-except
                _log.debug('roast history: %s %s', path, e)
        with self.lock:
            self.entries = entries
        if read:
            self.save_cache()
        return read

    def roasts(self) -> list[dict[str, Any]]:
        with self.lock:
            items = list(self.entries.values())
        return sorted(items, key=lambda r: (r.get('epoch') or 0), reverse=True) # newest first

    def beans(self) -> list[str]:
        seen:dict[str, str] = {}
        for r in self.roasts():
            n = normalize_bean(r['bean'])
            if n and n not in seen:
                seen[n] = r['bean']
        return list(seen.values())

    # roasts of the same bean (and, if given, a similar batch size), newest first
    def similar(self, bean:str, weight_in_g:float|None = None, exclude:str|None = None) -> list[dict[str, Any]]:
        b = normalize_bean(bean)
        out = []
        for r in self.roasts():
            if r['path'] == exclude or normalize_bean(r['bean']) != b:
                continue
            if weight_in_g and weight_in_g > 0 and not (r['weight_in_g'] and abs(r['weight_in_g'] - weight_in_g) <= WEIGHT_TOLERANCE * weight_in_g):
                continue
            out.append(r)
        return out

    # the background to roast against: the newest roast of the same bean and batch size, else of the same bean
    def best_background(self, bean:str, weight_in_g:float|None, exclude:str|None = None) -> dict[str, Any]|None:
        for candidates in (self.similar(bean, weight_in_g, exclude), self.similar(bean, None, exclude)):
            usable = [r for r in candidates if r.get('drop_time')]
            if usable:
                return usable[0]
        return None

    # medians of the phase marks over roasts of a similar batch size (any bean), for targets and coaching
    def batch_size_medians(self, weight_in_g:float) -> dict[str, Any]:
        rows = [r for r in self.roasts() if r['weight_in_g'] and abs(r['weight_in_g'] - weight_in_g) <= WEIGHT_TOLERANCE * weight_in_g]
        def med(key:str) -> float|None:
            v = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
            return statistics.median(v) if v else None
        return {'roasts': len(rows), 'dry_time': med('dry_time'), 'fcs_time': med('fcs_time'), 'drop_time': med('drop_time'),
            'dev_time': med('dev_time'), 'dtr_pct': med('dtr_pct'), 'loss_pct': med('loss_pct'), 'fcs_bt': med('fcs_bt'), 'charge_et': med('charge_et')}
