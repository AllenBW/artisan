#
# ABOUT
# Touch control panel for the event sliders actually used while roasting (by default fan and drum):
# large -/+ buttons in fixed steps, one-tap presets, run/stop, and the actual drive speed next to the setpoint.
# Every change goes through the slider path (moveslider + sliderReleased), so the MODBUS command, the event
# record and the slider/LCD updates are exactly those of moving the slider by hand. MODBUS writes are shown as
# sending until the device confirms them, and as not confirmed if it does not.
#
# Also keeps the bottom of the window clear of an auto-hidden taskbar and offers a roast screen display preset.
#
# Configuration (QSettings group 'TouchControls'): enabled, step, bottomSafeMargin, presetVersion

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

import re
import sys
import logging
from typing import override, Final, Any, TYPE_CHECKING

from PyQt6.QtCore import Qt, QObject, QEvent, QTimer, QSettings, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QSizePolicy, QMessageBox, QScrollArea)

from artisanlib.roast_assist import RoastAssist

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

# event type (slider) index -> section; presets are slider values, from the 154 logged roasts of this machine;
# 'on'/'off' name the custom event buttons whose actions start and stop the drive; 'label_scale' turns the value sent
# into the slider's unit for the logged event label (the drives take 0.1 Hz steps, confirmed on the drive's keypad:
# slider 30 sends 300 and the drive shows 30.0 Hz)
DEFAULT_SECTIONS: Final[list[dict[str, Any]]] = [
    {'slider': 0, 'name': 'Fan', 'presets': [20, 30, 50, 60, 80, 120], 'on': 'Fan On', 'off': 'Fan Off', 'label_scale': 0.1},
    {'slider': 1, 'name': 'Drum', 'presets': [20, 25, 30, 50], 'on': 'Drum On', 'off': 'Drum Off', 'label_scale': 0.1},
]
DEFAULT_STEP: Final[int] = 5
COMMIT_DELAY_MS: Final[int] = 700 # -/+ taps within this time are combined into one command and one event
CONFIRM_TIMEOUT_MS: Final[int] = 3000 # a write not confirmed within this time is shown as not confirmed
TASKBAR_PX: Final[int] = 48 # height of the Windows 11 taskbar that an auto-hidden taskbar shows over the window
PRESET_VERSION: Final[int] = 1 # bump to offer the display preset again on existing profiles
MODBUS_WRITE: Final = re.compile(r'write\w*\(\s*\[\s*(\d+)\s*,\s*(\d+)') # slave and register of a MODBUS write command

_instance: 'TouchControls|None' = None
extra_menu_actions: list[QAction] = [] # View menu actions of other fork modules, added again when the menu is rebuilt


def add_menu_action(aw:'ApplicationWindow', action:QAction) -> None:
    extra_menu_actions.append(action)
    if aw.viewMenu is not None and action not in aw.viewMenu.actions():
        aw.viewMenu.addAction(action)


def px_per_mm(widget:QWidget) -> float:
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is not None and screen.physicalSize().width() > 0:
        return screen.geometry().width() / screen.physicalSize().width()
    return 96 / 25.4


# the screen edge the Windows taskbar is on ('bottom', 'top', 'left', 'right'), None if unknown
def taskbar_edge() -> str|None:
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        class APPBARDATA(ctypes.Structure): # pylint: disable=too-few-public-methods
            _fields_ = [('cbSize', wintypes.DWORD), ('hWnd', wintypes.HWND), ('uCallbackMessage', wintypes.UINT),
                ('uEdge', wintypes.UINT), ('rc', wintypes.RECT), ('lParam', wintypes.LPARAM)]
        data = APPBARDATA()
        data.cbSize = ctypes.sizeof(APPBARDATA)
        if ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(data)): # ABM_GETTASKBARPOS
            return {0: 'left', 1: 'top', 2: 'right', 3: 'bottom'}.get(data.uEdge)
    except Exception as e: # pylint: disable=broad-except
        _log.debug(e)
    return None


class SliderSection(QFrame):
    """Controls for one event slider: setpoint and actual value, -/+ step buttons, presets, run/stop."""

    def __init__(self, aw:'ApplicationWindow', config:dict[str, Any], step:int, mm:float) -> None:
        super().__init__()
        self.aw = aw
        self.n:int = int(config['slider'])
        self.name:str = str(config['name'])
        self.step = step
        self.labels:dict[str, str] = {state: str(config[state]) for state in ('on', 'off') if config.get(state)}
        self.pending:int|None = None # target shown while -/+ taps are being collected
        self.sending:bool = False # a write was sent and is not yet confirmed
        self.problem:str = '' # last write that was not confirmed
        self.commit_timer = QTimer(self)
        self.commit_timer.setSingleShot(True)
        self.commit_timer.timeout.connect(self.commit)
        self.confirm_timer = QTimer(self)
        self.confirm_timer.setSingleShot(True)
        self.confirm_timer.timeout.connect(lambda: self.confirmed(False, 'no confirmation'))
        key = self.name.lower()
        self.setObjectName(f'touch_{key}')

        color = self.event_color()
        button_h = int(round(10 * mm)) # at least 10 mm tall for reliable finger taps
        self.setStyleSheet(f"""
            QFrame#touch_{key} {{ border: 1px solid rgba(128,128,128,0.35); border-radius: 8px; }}
            QFrame#touch_{key}[problem="true"] {{ border: 2px solid #e05a5a; }}
            QLabel#touch_{key}_title {{ font-size: {int(button_h * 0.42)}px; font-weight: bold; color: {color}; }}
            QLabel#touch_{key}_value {{ font-size: {int(button_h * 1.0)}px; font-weight: bold; }}
            QLabel#touch_{key}_actual {{ font-size: {int(button_h * 0.32)}px; color: rgba(160,160,160,1); }}
            QLabel#touch_{key}_actual[problem="true"] {{ color: #e05a5a; font-weight: bold; }}
            QPushButton {{ min-height: {button_h}px; font-size: {int(button_h * 0.36)}px; border-radius: 6px;
                border: 1px solid rgba(128,128,128,0.5); background: rgba(128,128,128,0.15); }}
            QPushButton:pressed {{ background: rgba(128,128,128,0.45); }}
            QPushButton:disabled {{ color: rgba(128,128,128,0.6); }}
            QPushButton[active="true"] {{ background: {color}; color: white; border-color: {color}; }}
            QPushButton#touch_{key}_on {{ background: rgba(40,160,70,0.85); color: white; }}
            QPushButton#touch_{key}_off {{ background: rgba(110,110,110,0.6); color: white; }}
            """)

        self.title = QLabel(self.name.upper())
        self.title.setObjectName(f'touch_{key}_title')
        self.value = QLabel('--')
        self.value.setObjectName(f'touch_{key}_value')
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.actual = QLabel('')
        self.actual.setObjectName(f'touch_{key}_actual')
        self.actual.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.actual.setWordWrap(True)

        minus = QPushButton(f'−{step}')
        minus.setObjectName(f'touch_{key}_minus')
        minus.clicked.connect(lambda: self.nudge(-self.step))
        plus = QPushButton(f'+{step}')
        plus.setObjectName(f'touch_{key}_plus')
        plus.clicked.connect(lambda: self.nudge(self.step))
        for b in (minus, plus):
            b.setMinimumWidth(int(round(16 * mm)))
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        value_row = QHBoxLayout()
        value_row.addWidget(minus)
        value_row.addWidget(self.value, 1)
        value_row.addWidget(plus)

        presets = QGridLayout()
        presets.setSpacing(int(round(1.5 * mm)))
        self.preset_buttons:dict[int, QPushButton] = {}
        for i, v in enumerate(config.get('presets', [])):
            b = QPushButton(str(v))
            b.setObjectName(f'touch_{key}_preset_{v}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, value=v: self.set_now(value))
            presets.addWidget(b, i // 4, i % 4)
            self.preset_buttons[int(v)] = b

        # Run / Stop share the title row, to keep the panel short enough for the screen
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        self.run_buttons:dict[str, QPushButton] = {}
        for state in self.labels:
            b = QPushButton('Run' if state == 'on' else 'Stop')
            b.setObjectName(f'touch_{key}_{state}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setMinimumWidth(int(round(16 * mm)))
            b.clicked.connect(lambda _=False, s=state: self.run(s))
            title_row.addWidget(b)
            self.run_buttons[state] = b

        layout = QVBoxLayout(self)
        layout.setSpacing(int(round(1.5 * mm)))
        layout.setContentsMargins(*([int(round(2 * mm))] * 4))
        layout.addLayout(title_row)
        layout.addLayout(value_row)
        layout.addWidget(self.actual)
        layout.addLayout(presets)

    def event_color(self) -> str:
        try:
            return str(self.aw.qmc.EvalueColor[self.n])
        except Exception: # pylint: disable=broad-except
            return '#2b7bb9'

    # slave and register the slider's MODBUS command writes to, None if it is not a MODBUS write
    def target(self) -> tuple[int, int]|None:
        try:
            if self.aw.eventslideractions[self.n] == 2: # MODBUS command
                m = MODBUS_WRITE.search(self.aw.eventslidercommands[self.n])
                if m:
                    return int(m.group(1)), int(m.group(2))
        except Exception: # pylint: disable=broad-except
            pass
        return None

    def limits(self) -> tuple[int, int]:
        return int(self.aw.eventslidermin[self.n]), int(self.aw.eventslidermax[self.n])

    def current(self) -> int:
        return int(self.aw.eventslidervalues[self.n])

    def clamp(self, v:int) -> int:
        lo, hi = self.limits()
        return max(lo, min(hi, v))

    # -/+ : show the new target at once, send it when the taps stop
    def nudge(self, delta:int) -> None:
        base = self.pending if self.pending is not None else self.current()
        self.pending = self.clamp(int(round((base + delta) / self.step)) * self.step)
        self.refresh()
        self.commit_timer.start(COMMIT_DELAY_MS)

    # presets are sent immediately
    def set_now(self, v:int) -> None:
        self.commit_timer.stop()
        self.pending = self.clamp(v)
        self.commit()

    def commit(self) -> None:
        if self.pending is None:
            return
        v, self.pending = self.pending, None
        if v == self.current() and not self.problem:
            self.refresh() # already set and confirmed: no second command, no second event
            return
        try:
            self.aw.moveslider(self.n, v)
            self.aw.sliderReleased(self.n, force=True, updateLCD=True) # sends the slider's command and records the event
            if self.target() is not None:
                self.sending = True
                self.confirm_timer.start(CONFIRM_TIMEOUT_MS)
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)
        self.refresh()

    # outcome of a MODBUS write to this section's device (called on the GUI thread)
    def on_write(self, slave:int, register:int, confirmed_ok:bool) -> None:
        target = self.target()
        if target is None or slave != target[0]:
            return
        if register == target[1]: # the speed (also when set with the slider)
            self.confirmed(confirmed_ok, 'not confirmed by the drive')
        else: # run/stop or another command to the same drive
            run_problem = f'{self.name} run/stop not confirmed by the drive'
            if not confirmed_ok:
                self.problem = run_problem
            elif self.problem == run_problem:
                self.problem = ''
            self.refresh()

    def confirmed(self, ok:bool, problem:str) -> None:
        self.sending = False
        self.confirm_timer.stop()
        self.problem = '' if ok else f'{self.name} {self.current()} {problem}'
        self.refresh()

    def extra_button(self, label:str) -> QPushButton|None:
        for b in self.aw.buttonlist:
            if b.text().replace('\n', ' ').strip() == label:
                return b
        return None

    def run(self, state:str) -> None:
        b = self.extra_button(self.labels[state])
        if b is not None:
            b.click() # runs the custom button's own action

    def actual_value(self) -> float|None:
        try:
            if self.aw.eventquantifieractive[self.n]:
                temp, _ = self.aw.quantifier2tempandtime(self.n)
                if temp is not None and len(temp) > 0 and temp[-1] not in (None, -1):
                    return float(temp[-1])
        except Exception: # pylint: disable=broad-except
            pass
        return None

    @staticmethod
    def set_property(w:QWidget, name:str, value:str) -> None:
        if w.property(name) != value:
            w.setProperty(name, value)
            style = w.style()
            if style is not None:
                style.unpolish(w)
                style.polish(w)

    def refresh(self) -> None:
        shown = self.pending if self.pending is not None else self.current()
        self.value.setText(f'{shown}' + (' …' if self.pending is not None else ''))
        if self.problem:
            text = self.problem
        elif self.sending:
            text = 'sending…'
        else:
            actual = self.actual_value()
            text = f'actual {actual:.1f}' if actual is not None and self.aw.qmc.flagon else ''
        self.actual.setText(text)
        self.set_property(self.actual, 'problem', 'true' if self.problem else 'false')
        self.set_property(self, 'problem', 'true' if self.problem else 'false')
        for v, b in self.preset_buttons.items():
            self.set_property(b, 'active', 'true' if v == shown else 'false')
        for state, b in self.run_buttons.items(): # a missing custom button disables run/stop instead of failing silently
            found = self.extra_button(self.labels[state]) is not None
            b.setEnabled(found)
            b.setToolTip('' if found else QApplication.translate('Message', 'No custom button "{0}"').format(self.labels[state]))


class WriteRelay(QObject): # pyright: ignore[reportGeneralTypeIssues]
    """Carries MODBUS write outcomes from the MODBUS thread to the GUI thread."""
    written = pyqtSignal(int, int, int, bool)


class TouchControls(QObject): # pyright: ignore[reportGeneralTypeIssues]
    def __init__(self, aw:'ApplicationWindow') -> None:
        super().__init__()
        self.aw = aw
        settings = QSettings()
        settings.beginGroup('TouchControls')
        self.enabled = bool(settings.value('enabled', True, type=bool))
        step = int(settings.value('step', DEFAULT_STEP, type=int))
        self.fixed_margin = int(settings.value('bottomSafeMargin', -1, type=int)) # -1: automatic
        preset_version = int(settings.value('presetVersion', 0, type=int))
        settings.endGroup()

        mm = px_per_mm(aw)
        # the panel scrolls when the screen is too short for it: its rows are never squeezed into each other
        self.panel = QScrollArea()
        self.panel.setObjectName('touchPanel')
        self.panel.setWidgetResizable(True)
        self.panel.setFrameShape(QFrame.Shape.NoFrame)
        self.panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QFrame()
        content.setObjectName('touchPanelContent')
        self.panel.setWidget(content)
        self.panel.setStyleSheet('QScrollArea#touchPanel, QFrame#touchPanelContent { background: transparent; }')
        viewport = self.panel.viewport()
        if viewport is not None:
            viewport.setAutoFillBackground(False)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(int(mm), int(mm), int(mm), int(mm))
        self.sections = [SliderSection(aw, config, step, mm) for config in DEFAULT_SECTIONS
            if config['slider'] < len(aw.eventslidervisibilities) and aw.eventslidervisibilities[config['slider']]]
        by_name = {s.name.lower(): s for s in self.sections}
        self.assist = RoastAssist(aw, mm,
            by_name['fan'].set_now if 'fan' in by_name else None, by_name['drum'].set_now if 'drum' in by_name else None)
        layout.addWidget(self.assist.guide)
        for s in self.sections:
            layout.addWidget(s)
        layout.addWidget(self.assist.box)
        layout.addStretch(1) # nothing important at the bottom edge
        self.content = content
        self.min_width = int(round(52 * mm))
        self.fit_width()
        self.panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        for config in DEFAULT_SECTIONS:
            if config['slider'] < len(aw.eventsliderdescriptionscale) and aw.eventsliderfactors[config['slider']] == 10:
                aw.eventsliderdescriptionscale[config['slider']] = config['label_scale']

        # the heating element buttons in the empty part of the top row, right after the toolbar: anchored there, they
        # don't move when the phase LCDs further right change width (as they do with START); the row grows to fit
        # touch-sized buttons
        index = aw.level1layout.indexOf(aw.phasesLCDsFrame)
        if index >= 1: # after the stretch before the phase LCDs
            aw.level1layout.insertWidget(index - 1, self.assist.elements_bar)
            aw.level1frame.setFixedHeight(max(aw.level1frame.minimumHeight(), int(round(9 * mm)) + 7 + 12 + 6)) # + the row's margins

        # part of the main layout, right of the LCDs (a dock widget would be placed by the window state restored before)
        aw.midlayout.addWidget(self.panel)
        self.panel.setVisible(self.enabled and bool(self.sections))
        self.assist.elements_bar.setVisible(not self.panel.isHidden())

        # write outcomes from the MODBUS thread
        self.relay = WriteRelay()
        self.relay.written.connect(self.on_write)
        aw.modbus.write_listeners.append(self.relay.written.emit)

        self.action = QAction(QApplication.translate('Menu', 'Touch Controls'), aw)
        self.action.setCheckable(True)
        self.action.setChecked(self.panel.isVisible())
        self.action.triggered.connect(self.toggle)
        self.preset_action = QAction(QApplication.translate('Menu', 'Roast Screen Display Preset'), aw)
        self.preset_action.triggered.connect(lambda _=False: self.apply_display_preset())
        self.ensure_menu()

        # keep the bottom rows out of the taskbar strip, also after the window was moved, resized or maximized
        self.taskbar = taskbar_edge()
        self.margin:int|None = None
        aw.installEventFilter(self)
        self.update_bottom_margin()

        if preset_version < PRESET_VERSION:
            QTimer.singleShot(1500, self.offer_display_preset)

        self.timer = QTimer(aw)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def refresh(self) -> None:
        self.ensure_menu()
        self.assist.refresh() # also records the element state at CHARGE when the panel is hidden
        if self.panel.isVisible():
            for s in self.sections:
                s.refresh()
            self.fit_width()

    # wide enough for the content plus a scrollbar, so the content is never cut off on the right should it scroll
    def fit_width(self) -> None:
        scrollbar = self.panel.verticalScrollBar()
        width = max(self.min_width, self.content.minimumSizeHint().width()) + (scrollbar.sizeHint().width() if scrollbar is not None else 0)
        if self.panel.minimumWidth() != width:
            self.panel.setFixedWidth(width)

    # the View menu is rebuilt on a UI mode change; add the actions again when they are missing
    def ensure_menu(self) -> None:
        menu = self.aw.viewMenu
        if menu is not None and self.action not in menu.actions():
            menu.addSeparator()
            menu.addAction(self.action)
            menu.addAction(self.preset_action)
            for a in extra_menu_actions:
                menu.addAction(a)

    def on_write(self, slave:int, register:int, _value:int, ok:bool) -> None:
        for s in self.sections:
            s.on_write(slave, register, ok)

    def toggle(self, checked:bool = False) -> None:
        self.panel.setVisible(checked and bool(self.sections))
        self.assist.elements_bar.setVisible(not self.panel.isHidden())
        QSettings().setValue('TouchControls/enabled', checked)

    @override
    def eventFilter(self, obj:QObject|None, event:QEvent|None) -> bool: # pylint: disable=invalid-name
        if obj is self.aw and event is not None and event.type() in {QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.WindowStateChange}:
            QTimer.singleShot(0, self.update_bottom_margin)
        return False

    # the part of the window that lies in the strip an auto-hidden bottom taskbar slides over is kept free
    def update_bottom_margin(self) -> None:
        margin = self.fixed_margin
        if margin < 0:
            margin = 0
            screen = self.aw.screen()
            if screen is not None and self.taskbar in {None, 'bottom'}:
                geometry, available = screen.geometry(), screen.availableGeometry()
                if available.bottom() >= geometry.bottom() - 1: # the taskbar does not reserve space: it is shown over windows
                    strip_top = geometry.bottom() - TASKBAR_PX
                    margin = max(0, min(TASKBAR_PX, self.aw.frameGeometry().bottom() - strip_top))
        if margin != self.margin:
            self.margin = margin
            layout = self.aw.main_widget.layout()
            if layout is not None:
                m = layout.contentsMargins()
                layout.setContentsMargins(m.left(), m.top(), m.right(), margin)

    def offer_display_preset(self) -> None:
        box = QMessageBox(self.aw)
        box.setObjectName('touchPresetPrompt')
        box.setWindowTitle(QApplication.translate('Message', 'Roast screen display preset'))
        box.setText(QApplication.translate('Message', 'Apply the roast screen display preset?\n\n'
            'Thicker BT/ET/RoR curves, temperature axis 50-240 °C, 15 minute time axis, and fan/drum sliders in steps of 5. '
            'It can be applied later from View > Roast Screen Display Preset.'))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        def answered(result:int) -> None:
            if result == QMessageBox.StandardButton.Yes:
                self.apply_display_preset()
            QSettings().setValue('TouchControls/presetVersion', PRESET_VERSION) # asked once per preset version
        box.finished.connect(answered)
        box.open()

    # thicker main curves, temperature and time axes fitted to this machine's roasts, sliders snapping to the step
    def apply_display_preset(self) -> None:
        qmc = self.aw.qmc
        try:
            qmc.BTlinewidth = 4.0
            qmc.ETlinewidth = 3.0
            qmc.BTdeltalinewidth = 2.5
            if qmc.mode == 'C':
                qmc.ylimit_min, qmc.ylimit = 50, 240
            else:
                qmc.ylimit_min, qmc.ylimit = 120, 460
            qmc.resetmaxtime = 15 * 60
            qmc.endofx = max(qmc.endofx, 15 * 60)
            for s in self.sections:
                self.aw.eventslidercoarse[s.n] = 2 # step size 5
            self.aw.updateSlidersProperties()
            qmc.redraw(recomputeAllDeltas=False, forceRenewAxis=True)
            self.aw.sendmessage(QApplication.translate('Message', 'Roast screen display preset applied'))
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)


def install(aw:'ApplicationWindow') -> None:
    global _instance # pylint: disable=global-statement
    if _instance is None and not aw.app.artisanviewerMode:
        try:
            _instance = TouchControls(aw)
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)
