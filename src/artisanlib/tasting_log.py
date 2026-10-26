#
# ABOUT
# Tasting and grind-setting log: how each roast tasted, brewed how (method, grinder, grind setting, dose, yield,
# time), with a score and the development verdict, so that roast decisions can be checked in the cup.
#
# Stored as one JSON object per line in tasting-log.jsonl next to the roasts (the shared roast folder), so it travels
# with them; append-only, a line that cannot be read is skipped. QSettings 'RoastAssist/tastingLog' overrides the path.

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
import json
import uuid
import logging
import datetime
import threading
from pathlib import Path
from typing import Final, Any, TYPE_CHECKING, override

from PyQt6.QtCore import Qt, QSettings, QTimer
from PyQt6.QtGui import QShowEvent
from PyQt6.QtWidgets import (QApplication, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout, QDialog,
    QComboBox, QLineEdit, QListWidget, QListWidgetItem, QButtonGroup, QScrollArea, QWidget)

from artisanlib.roast_history import normalize_bean

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import
    from artisanlib.roast_assist import RoastAssist # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

FILE_NAME: Final[str] = 'tasting-log.jsonl'
METHODS: Final[list[str]] = ['Espresso', 'Pour-over', 'Batch brew', 'French press', 'AeroPress', 'Cupping']
VERDICTS: Final[list[tuple[str, str]]] = [('under', 'Under-developed'), ('right', 'Right'), ('over', 'Over-developed')]
DESCRIPTORS: Final[list[str]] = ['Sour', 'Bright', 'Sweet', 'Balanced', 'Bitter', 'Ashy', 'Grassy', 'Flat', 'Astringent']
NUMBER_FIELDS: Final[list[tuple[str, str]]] = [('grind', 'Grind'), ('dose_g', 'Dose g'), ('yield_g', 'Yield g'),
    ('time_s', 'Time s'), ('water_c', 'Water °C')]


def mmss(seconds:float|None) -> str:
    if seconds is None:
        return '--:--'
    s = max(0, int(round(seconds)))
    return f'{s // 60}:{s % 60:02d}'


def roast_key(name:str) -> str:
    """The batch number at the start of a roast name ('160 India Natural ...' -> '160'), else the whole name."""
    first = str(name or '').strip().split(' ')[0]
    return first if first.isdigit() else str(name or '').strip()


def days_off_roast(roast_date:str, when:datetime.date) -> int|None:
    try:
        return (when - datetime.date.fromisoformat(str(roast_date)[:10])).days
    except ValueError:
        return None


class TastingLog:
    """Thread-safe, append-only JSON-lines log of tastings."""

    def __init__(self, path:str) -> None:
        self.path = path
        self.lock = threading.Lock()

    def entries(self) -> list[dict[str, Any]]:
        out:list[dict[str, Any]] = []
        with self.lock:
            if not self.path or not os.path.isfile(self.path):
                return out
            try:
                with open(self.path, encoding='utf-8') as f:
                    for raw in f:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                            if isinstance(e, dict):
                                out.append(e)
                        except ValueError:
                            _log.debug('tasting log: skipped an unreadable line')
            except OSError as e:
                _log.debug(e)
        return out

    def add(self, entry:dict[str, Any]) -> dict[str, Any]:
        e = {k: v for k, v in entry.items() if v not in (None, '', [])}
        e.setdefault('id', uuid.uuid4().hex[:12])
        e.setdefault('recorded', datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
        with self.lock:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
        return e

    # newest first
    def query(self, roast:str = '', bean:str = '', method:str = '', limit:int = 50) -> list[dict[str, Any]]:
        rows = []
        for e in reversed(self.entries()):
            if roast and roast_key(e.get('roast', '')) != roast_key(roast):
                continue
            if bean and normalize_bean(bean) not in normalize_bean(e.get('bean', '')):
                continue
            if method and str(e.get('method', '')).lower() != method.lower():
                continue
            rows.append(e)
            if len(rows) >= limit:
                break
        return rows

    def for_roast(self, name:str) -> list[dict[str, Any]]:
        return self.query(roast=name, limit=100)

    def grinders(self) -> list[str]:
        seen:list[str] = []
        for e in reversed(self.entries()):
            g = str(e.get('grinder', '')).strip()
            if g and g not in seen:
                seen.append(g)
        return seen


def default_path(history_folder:str) -> str:
    stored = str(QSettings().value('RoastAssist/tastingLog', '', type=str) or '')
    if stored:
        return stored
    folder = history_folder if history_folder and os.path.isdir(history_folder) else os.path.expanduser('~')
    return os.path.join(folder, FILE_NAME)


def describe(e:dict[str, Any]) -> str:
    """One line for a tasting, eg. 'Espresso · Niche 12 · 18→36 g in 28 s · 4/5 · right · sweet, bright'."""
    parts = [str(e.get('method', ''))]
    g = ' '.join(str(x) for x in (e.get('grinder', ''), e.get('grind', '')) if x)
    if g:
        parts.append(g)
    dose, out, t = e.get('dose_g'), e.get('yield_g'), e.get('time_s')
    if dose and out:
        parts.append(f'{dose:g}→{out:g} g' + (f' in {t:g} s' if t else '') + f' (1:{out / dose:.1f})')
    elif t:
        parts.append(f'{t:g} s')
    if e.get('score'):
        parts.append(f"{e['score']}/5")
    if e.get('development'):
        parts.append(str(e['development']))
    if e.get('descriptors'):
        parts.append(', '.join(e['descriptors']).lower())
    if e.get('notes'):
        parts.append(f"“{e['notes']}”")
    return ' · '.join(p for p in parts if p)


class TastingDialog(QDialog):
    """Log a tasting of a past roast with the brew and grind settings; shows the last tasting of the same bean and
    method to start from."""

    def __init__(self, aw:'ApplicationWindow', assist:'RoastAssist', mm:float) -> None:
        super().__init__(aw)
        from artisanlib.roast_assist import touch_style
        self.aw = aw
        self.assist = assist
        self.log = assist.tastings
        self.setWindowTitle(QApplication.translate('Form Caption', 'Tasting and grind log'))
        h = int(round(9 * mm))
        self.setStyleSheet(touch_style(mm) + ' QPushButton:checked { background: rgba(224,90,60,0.9); color: white; }')
        s = QSettings()
        last_method = str(s.value('RoastAssist/tastingMethod', 'Espresso', type=str))
        last_grinder = str(s.value('RoastAssist/tastingGrinder', '', type=str))

        # roast to taste
        self.roasts = QListWidget()
        self.roasts.setObjectName('tasting_roasts')
        self.roasts.setMaximumHeight(int(3.2 * h))
        today = datetime.datetime.now().astimezone().date()
        for r in assist.history.roasts()[:40]:
            age = days_off_roast(r.get('date', ''), today)
            n = len(self.log.for_roast(r['name']))
            text = (f"#{roast_key(r['name'])} {r['bean']} · {r.get('date', '')}" + (f' · {age} d off roast' if age is not None else '')
                + (f' · {n} tasting' + ('s' if n != 1 else '') if n else ''))
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, r)
            self.roasts.addItem(item)
        self.roast_info = QLabel()
        self.roast_info.setObjectName('tasting_roast_info')
        self.roast_info.setWordWrap(True)
        self.roast_info.setTextFormat(Qt.TextFormat.RichText)

        # method
        self.method_group = QButtonGroup(self)
        self.method_group.setExclusive(True)
        mrow = QHBoxLayout()
        for m in METHODS:
            b = QPushButton(m)
            b.setObjectName(f"tasting_method_{m.lower().replace(' ', '_').replace('-', '_')}")
            b.setCheckable(True)
            b.setChecked(m == last_method)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.method_group.addButton(b)
            mrow.addWidget(b)
        self.method_group.buttonClicked.connect(lambda _: self.update_last())

        # grinder and numbers
        self.grinder = QComboBox()
        self.grinder.setObjectName('tasting_grinder')
        self.grinder.setEditable(True)
        self.grinder.addItems(self.log.grinders())
        self.grinder.setCurrentText(last_grinder)
        self.fields:dict[str, QLineEdit] = {}
        grid = QGridLayout()
        for col, (key, label) in enumerate(NUMBER_FIELDS):
            b = QPushButton(label)
            b.setObjectName(f'tasting_select_{key}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            f = QLineEdit()
            f.setObjectName(f'tasting_{key}')
            f.setAlignment(Qt.AlignmentFlag.AlignCenter)
            b.clicked.connect(lambda _=False, w=f: self.select(w))
            grid.addWidget(b, 0, col)
            grid.addWidget(f, 1, col)
            self.fields[key] = f
        self.target:QLineEdit = self.fields['grind']
        # keypad (2 rows of 6) with the grind steps at its right end
        keypad = QGridLayout()
        for i, key in enumerate(['7', '8', '9', '4', '5', '6', '1', '2', '3', '.', '0', '⌫']):
            b = QPushButton(key)
            b.setObjectName(f'tasting_key_{key}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, k=key: self.key(k))
            keypad.addWidget(b, i // 6, i % 6)
        for i, (label, delta) in enumerate((('−1', -1.0), ('+1', 1.0), ('−0.5', -0.5), ('+0.5', 0.5))):
            b = QPushButton(label)
            b.setObjectName(f'tasting_step_{label}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, d=delta: self.step(d))
            keypad.addWidget(b, i // 2, 6 + i % 2)

        self.last = QLabel()
        self.last.setObjectName('tasting_last')
        self.last.setWordWrap(True)
        self.use_last = QPushButton('Start from last')
        self.use_last.setObjectName('tasting_use_last')
        self.use_last.clicked.connect(self.fill_last)
        self.last_entry:dict[str, Any]|None = None

        # verdicts
        self.score_group = QButtonGroup(self)
        srow = QHBoxLayout()
        srow.addWidget(QLabel('Score'))
        for n in range(1, 6):
            b = QPushButton(str(n))
            b.setObjectName(f'tasting_score_{n}')
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.score_group.addButton(b, n)
            srow.addWidget(b)
        self.dev_group = QButtonGroup(self)
        drow = QHBoxLayout()
        drow.addWidget(QLabel('Development'))
        for i, (key, label) in enumerate(VERDICTS):
            b = QPushButton(label)
            b.setObjectName(f'tasting_dev_{key}')
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.dev_group.addButton(b, i)
            drow.addWidget(b)
        self.descriptor_buttons:list[QPushButton] = []
        crow = QGridLayout()
        for i, d in enumerate(DESCRIPTORS):
            b = QPushButton(d)
            b.setObjectName(f'tasting_taste_{d.lower()}')
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            crow.addWidget(b, 0, i)
            self.descriptor_buttons.append(b)
        self.notes = QLineEdit()
        self.notes.setObjectName('tasting_notes')
        self.notes.setPlaceholderText('notes: what you tasted, what to change next time')
        self.message = QLabel()
        self.message.setObjectName('tasting_message')
        self.message.setWordWrap(True)

        save = QPushButton(QApplication.translate('Button', 'Save'))
        save.setObjectName('tasting_save')
        save.setDefault(True)
        save.clicked.connect(self.save)
        close = QPushButton(QApplication.translate('Button', 'Close'))
        close.setObjectName('tasting_close')
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.message, 1)
        buttons.addWidget(close)
        buttons.addWidget(save)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(QLabel('Roast'))
        layout.addWidget(self.roasts)
        layout.addWidget(self.roast_info)
        layout.addWidget(QLabel('Brew method'))
        layout.addLayout(mrow)
        lrow = QHBoxLayout()
        lrow.addWidget(self.last, 1)
        lrow.addWidget(self.use_last)
        layout.addLayout(lrow)
        grow = QHBoxLayout()
        grow.addWidget(QLabel('Grinder'))
        grow.addWidget(self.grinder, 1)
        layout.addLayout(grow)
        layout.addLayout(grid)
        layout.addLayout(keypad)
        vrow = QHBoxLayout() # score and development verdict share a row
        vrow.addLayout(srow, 5)
        vrow.addSpacing(int(3 * mm))
        vrow.addLayout(drow, 6)
        layout.addLayout(vrow)
        layout.addWidget(QLabel('Taste'))
        layout.addLayout(crow)
        layout.addWidget(self.notes)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addLayout(buttons)
        self.mm = mm
        self.body = body
        self.save_button = save
        self.resize(int(200 * mm), int(165 * mm))

        self.roasts.currentRowChanged.connect(lambda _: self.update_roast())
        if self.roasts.count():
            self.roasts.setCurrentRow(self.default_row())
        self.update_roast()

    # as tall as the content, up to the screen (it scrolls only on a smaller screen); measured once shown, when the
    # touch-size style sheet has been applied to the buttons
    @override
    def showEvent(self, a0:QShowEvent|None) -> None: # pylint: disable=invalid-name
        super().showEvent(a0)
        QTimer.singleShot(0, self.fit_height)

    def fit_height(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry().height() - self.height()
        wanted = self.body.sizeHint().height() + self.save_button.sizeHint().height() + int(8 * self.mm)
        height = min(available.height() - frame, wanted)
        self.resize(self.width(), height)
        top = max(available.top(), min(self.frameGeometry().top(), available.bottom() - height - frame))
        self.move(self.frameGeometry().left(), top)

    # the roast most likely tasted now: the newest one that is at least a day old, else the newest
    def default_row(self) -> int:
        today = datetime.datetime.now().astimezone().date()
        for i in range(self.roasts.count()):
            item = self.roasts.item(i)
            r = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            age = days_off_roast(r.get('date', ''), today) if r else None
            if age is not None and age >= 1:
                return i
        return 0

    def roast(self) -> dict[str, Any]|None:
        item = self.roasts.currentItem()
        r = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return r if isinstance(r, dict) else None

    def method(self) -> str:
        b = self.method_group.checkedButton()
        return b.text() if b is not None else ''

    def update_roast(self) -> None:
        r = self.roast()
        if r is None:
            self.roast_info.setText('No roasts found in the roast folder.')
            self.update_last()
            return
        bits = [f"{int(r.get('weight_in_g') or 0)} g"]
        if r.get('fcs_time'):
            bits.append(f"FC {mmss(r['fcs_time'])}")
        if r.get('drop_time'):
            bits.append(f"drop {mmss(r['drop_time'])}")
        if r.get('dev_time') and r.get('dtr_pct') is not None:
            bits.append(f"development {mmss(r['dev_time'])} ({r['dtr_pct']:.0f} %)")
        if r.get('loss_pct') is not None:
            bits.append(f"loss {r['loss_pct']:.1f} %")
        earlier = self.log.for_roast(r['name'])
        text = ' · '.join(bits)
        if earlier:
            text += f'<br>Tasted before ({len(earlier)}): ' + describe(earlier[0])
        self.roast_info.setText(text)
        self.update_last()

    def update_last(self) -> None:
        r = self.roast()
        method = self.method()
        self.last_entry = None
        if r is not None and method:
            same = self.log.query(bean=r.get('bean', ''), method=method, limit=1) or self.log.query(method=method, limit=1)
            if same:
                self.last_entry = same[0]
        if self.last_entry is not None:
            e = self.last_entry
            which = 'this bean' if r and normalize_bean(e.get('bean', '')) == normalize_bean(r.get('bean', '')) else 'any bean'
            self.last.setText(f"Last {method.lower()} ({which}, #{roast_key(e.get('roast', ''))}): {describe(e)}")
        else:
            self.last.setText(f'No earlier {method.lower()} tasting.' if method else '')
        self.use_last.setEnabled(self.last_entry is not None)

    def fill_last(self) -> None:
        e = self.last_entry
        if e is None:
            return
        if e.get('grinder'):
            self.grinder.setCurrentText(str(e['grinder']))
        for key, f in self.fields.items():
            v = e.get(key)
            f.setText(f'{v:g}' if isinstance(v, (int, float)) else str(v or ''))

    def select(self, field:QLineEdit) -> None:
        self.target = field
        field.setFocus()

    def key(self, k:str) -> None:
        text = self.target.text()
        if k == '⌫':
            self.target.setText(text[:-1])
        elif k != '.' or '.' not in text:
            self.target.setText(text + k)

    def step(self, delta:float) -> None:
        f = self.target
        try:
            v = float(f.text().replace(',', '.') or 0)
        except ValueError:
            v = 0.0
        f.setText(f'{max(0.0, v + delta):g}')

    @staticmethod
    def number(text:str) -> float|str|None:
        t = text.strip().replace(',', '.')
        if not t:
            return None
        try:
            return float(t)
        except ValueError:
            return text.strip() # a grind setting can be like '2.3.1' or 'A5'

    def entry(self) -> dict[str, Any]|None:
        r = self.roast()
        if r is None:
            return None
        e:dict[str, Any] = {
            'roast': r['name'], 'bean': r.get('bean', ''), 'roast_date': r.get('date', ''),
            'days_off_roast': days_off_roast(r.get('date', ''), datetime.datetime.now().astimezone().date()),
            'method': self.method(), 'grinder': self.grinder.currentText().strip(),
        }
        for key, f in self.fields.items():
            e[key] = self.number(f.text())
        if self.score_group.checkedId() > 0:
            e['score'] = self.score_group.checkedId()
        if self.dev_group.checkedId() >= 0:
            e['development'] = VERDICTS[self.dev_group.checkedId()][0]
        e['descriptors'] = [b.text() for b in self.descriptor_buttons if b.isChecked()]
        e['notes'] = ' '.join(self.notes.text().split())
        return e

    def save(self) -> None:
        e = self.entry()
        if e is None:
            self.message.setText('Pick a roast first.')
            return
        try:
            saved = self.log.add(e)
        except OSError as err:
            self.message.setText(f'Not saved: {err}')
            return
        s = QSettings()
        s.setValue('RoastAssist/tastingMethod', e.get('method', ''))
        s.setValue('RoastAssist/tastingGrinder', e.get('grinder', ''))
        self.aw.sendmessage(f"Tasting saved: #{roast_key(e['roast'])} {describe(saved)}")
        self.assist.tasting_saved(saved)
        self.accept()
