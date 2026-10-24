#
# ABOUT
# Optional logger of user interactions (taps, clicks, keys, sliders, dialogs, roast marks)
# written as JSON Lines to be lined up with a screen recording of the same session.
#
# Enabled by setting the environment variable ARTISAN_INTERACTION_LOG=1.
# Files are written to ARTISAN_SESSION_DIR (default: ~/artisan-sessions).
# Text typed into input fields is never recorded, only that typing happened.

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
import time
import datetime
import logging
from pathlib import Path
from typing import override, Final, Any, TextIO, TYPE_CHECKING

from PyQt6.QtCore import Qt, QObject, QEvent, QPoint, QTimer
from PyQt6.QtGui import QMouseEvent, QKeyEvent, QWheelEvent, QTouchEvent, QKeySequence, QPointerEvent
from PyQt6.QtWidgets import (QApplication, QWidget, QDialog, QMenu, QAbstractButton, QAbstractSlider, QComboBox,
    QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QTabBar)

from artisanlib import __version__

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

_MARK_NAMES: Final[list[str]] = ['CHARGE', 'DRY', 'FCs', 'FCe', 'SCs', 'SCe', 'DROP', 'COOL']
_POINTER_EVENTS: Final[set[QEvent.Type]] = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick, QEvent.Type.TouchBegin, QEvent.Type.TouchEnd, QEvent.Type.TouchCancel}
_TEXT_INPUTS: Final[tuple[type, ...]] = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)

_instance: 'InteractionLogger|None' = None # keeps the logger alive for the lifetime of the app


def enabled() -> bool:
    return os.environ.get('ARTISAN_INTERACTION_LOG', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def session_dir() -> Path:
    d = os.environ.get('ARTISAN_SESSION_DIR', '').strip()
    return Path(d) if d else Path.home() / 'artisan-sessions'


def install(aw:'ApplicationWindow') -> None:
    global _instance # pylint: disable=global-statement
    if _instance is None and enabled():
        try:
            _instance = InteractionLogger(aw)
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)


class InteractionLogger(QObject): # pyright: ignore[reportGeneralTypeIssues]

    def __init__(self, aw:'ApplicationWindow') -> None:
        super().__init__()
        self.aw = aw
        directory = session_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.path:Path = directory / f'interactions-{datetime.datetime.now(datetime.UTC).astimezone():%Y%m%d-%H%M%S}.jsonl'
        self.file:TextIO|None = self.path.open('a', encoding='utf-8', buffering=1) # line buffered: nothing lost on a crash
        self.last_key:tuple[Any, ...]|None = None # (event type, timestamp, position) of the last logged event, to skip re-deliveries to parent widgets
        self.last_wheel:float = 0
        self.last_state:tuple[bool, bool] = (False, False)
        self.marks_seen:list[bool] = [False]*len(_MARK_NAMES)
        self.errors:int = 0

        screens = [{'name': s.name(), 'geometry': [s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height()],
            'dpr': s.devicePixelRatio()} for s in QApplication.screens()]
        self.write({'type': 'session_start', 'version': __version__, 'app': QApplication.applicationName(), 'screens': screens})

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            app.aboutToQuit.connect(self.close)
        # poll the roast state to log ON/OFF, START/STOP and event marks (CHARGE, DRY,..) as anchors on the timeline
        self.state_timer = QTimer(self)
        self.state_timer.timeout.connect(self.poll_state)
        self.state_timer.start(250)
        _log.info('interaction log: %s', self.path)

    def write(self, record:dict[str, Any]) -> None:
        if self.file is None:
            return
        now = time.time()
        record = {'ts': datetime.datetime.fromtimestamp(now, datetime.UTC).astimezone().isoformat(timespec='milliseconds'), 'epoch': round(now, 3), **record}
        ctx = self.roast_context()
        if ctx:
            record['roast'] = ctx
        self.file.write(json.dumps(record, ensure_ascii=False) + '\n')

    def close(self) -> None:
        if self.file is not None:
            self.write({'type': 'session_end'})
            self.file.close()
            self.file = None

    # recording time and time since CHARGE in seconds, as shown on the LCDs
    def roast_context(self) -> dict[str, Any]:
        try:
            qmc = self.aw.qmc
            ctx:dict[str, Any] = {}
            if qmc.flagon:
                ctx['on'] = True
            if qmc.flagstart:
                t = qmc.timeclock.elapsedMilli()
                ctx['t_rec'] = round(t, 1)
                if qmc.timeindex[0] > -1 and len(qmc.timex) > qmc.timeindex[0]:
                    ctx['t_charge'] = round(t - qmc.timex[qmc.timeindex[0]], 1)
            return ctx
        except Exception: # pylint: disable=broad-except
            return {}

    def poll_state(self) -> None:
        try:
            qmc = self.aw.qmc
            state = (bool(qmc.flagon), bool(qmc.flagstart))
            if state != self.last_state:
                if state[0] != self.last_state[0]:
                    self.write({'type': 'state', 'what': 'ON' if state[0] else 'OFF'})
                if state[1] != self.last_state[1]:
                    self.write({'type': 'state', 'what': 'START' if state[1] else 'STOP'})
                    self.marks_seen = [False]*len(_MARK_NAMES)
                self.last_state = state
            if qmc.flagstart:
                for i, name in enumerate(_MARK_NAMES):
                    is_set = qmc.timeindex[i] > -1 if i == 0 else qmc.timeindex[i] > 0
                    if is_set != self.marks_seen[i]:
                        self.marks_seen[i] = is_set
                        self.write({'type': 'mark' if is_set else 'mark_undo', 'what': name})
        except Exception as e: # pylint: disable=broad-except
            self.log_error(e)

    def log_error(self, e:Exception) -> None:
        self.errors += 1
        if self.errors <= 5:
            _log.exception(e)

    @staticmethod
    def describe(w:QWidget, pos:QPoint|None = None) -> dict[str, Any]:
        d:dict[str, Any] = {'class': type(w).__name__}
        if w.objectName():
            d['name'] = w.objectName()
        text = ''
        if isinstance(w, QAbstractButton):
            text = w.text()
        elif isinstance(w, QComboBox):
            text = w.currentText()
        elif isinstance(w, QTabBar) and pos is not None:
            text = w.tabText(w.tabAt(pos))
        elif isinstance(w, QMenu) and pos is not None:
            action = w.actionAt(pos)
            if action is not None:
                text = action.text()
        if text:
            d['text'] = text.replace('\n', ' ').replace('&', '')[:60]
        if isinstance(w, QAbstractSlider):
            d['value'] = w.value()
        if w.toolTip():
            d['tip'] = w.toolTip()[:60]
        origin = w.mapToGlobal(QPoint(0, 0))
        d['rect'] = [origin.x(), origin.y(), w.width(), w.height()] # global coordinates, to judge touch target sizes and near misses
        parents:list[str] = []
        p = w.parentWidget()
        while p is not None and len(parents) < 4 and not p.isWindow():
            parents.append(p.objectName() or type(p).__name__)
            p = p.parentWidget()
        if parents:
            d['parents'] = parents
        win = w.window()
        if win is not None:
            d['window'] = win.windowTitle() or type(win).__name__
        return d

    @staticmethod
    def device(event:QPointerEvent) -> str:
        dev = event.pointingDevice()
        return (dev.type().name or 'unknown') if dev is not None else 'unknown' # eg. TouchScreen, Mouse, Stylus

    @override
    def eventFilter(self, obj:QObject|None, event:QEvent|None) -> bool: # pylint: disable=invalid-name
        if event is None or not isinstance(obj, QWidget) or self.file is None:
            return False
        try:
            etype = event.type()
            if etype in _POINTER_EVENTS and isinstance(event, QPointerEvent):
                points = event.points()
                gpos = points[0].globalPosition().toPoint() if points else QPoint()
                key:tuple[Any, ...] = (etype, event.timestamp(), gpos.x(), gpos.y())
                if key != self.last_key: # an ignored event is re-delivered to the parents; only log the widget under the finger
                    self.last_key = key
                    record:dict[str, Any] = {'type': etype.name, 'device': self.device(event), 'pos': [gpos.x(), gpos.y()],
                        'widget': self.describe(obj, obj.mapFromGlobal(gpos))}
                    if isinstance(event, QMouseEvent):
                        record['button'] = event.button().name
                    elif isinstance(event, QTouchEvent):
                        record['points'] = len(points)
                    self.write(record)
            elif etype == QEvent.Type.KeyPress and isinstance(event, QKeyEvent) and not event.isAutoRepeat():
                key = (etype, event.timestamp(), event.key(), -1)
                if key != self.last_key:
                    self.last_key = key
                    mods = event.modifiers()
                    typing = any(isinstance(w, _TEXT_INPUTS) or (isinstance(w, QComboBox) and w.isEditable())
                            for w in (obj, QApplication.focusWidget())) and \
                        event.text().isprintable() and event.text() != '' and \
                        not mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
                    self.write({'type': 'KeyPress', 'key': '<text>' if typing else QKeySequence(event.keyCombination()).toString(),
                        'widget': self.describe(obj)})
            elif etype == QEvent.Type.Wheel and isinstance(event, QWheelEvent):
                now = time.monotonic()
                if now - self.last_wheel > 0.5: # throttled: one record per scroll gesture
                    self.last_wheel = now
                    gpos = event.globalPosition().toPoint()
                    self.write({'type': 'Wheel', 'device': self.device(event), 'delta': event.angleDelta().y(),
                        'pos': [gpos.x(), gpos.y()], 'widget': self.describe(obj)})
            elif etype in {QEvent.Type.Show, QEvent.Type.Hide} and isinstance(obj, (QDialog, QMenu)) and obj.isWindow():
                self.write({'type': 'window_' + ('open' if etype == QEvent.Type.Show else 'close'), 'widget': self.describe(obj)})
        except Exception as e: # pylint: disable=broad-except
            self.log_error(e)
        return False # never consume events
