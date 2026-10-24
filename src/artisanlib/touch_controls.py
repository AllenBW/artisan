#
# ABOUT
# Touch control panel for the event sliders actually used while roasting (by default fan and drum):
# large -/+ buttons in fixed steps, one-tap presets, run/stop, and the actual drive speed next to the setpoint.
# Every change goes through the slider path (moveslider + sliderReleased), so the MODBUS command, the event
# record and the slider/LCD updates are exactly those of moving the slider by hand.
#
# Also keeps the bottom of the window clear of an auto-hidden taskbar and applies a roast screen display preset.
#
# Configuration (QSettings group 'TouchControls'): enabled, step, sections, presets, bottomSafeMargin, presetVersion

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

import logging
from typing import Final, Any, TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer, QSettings
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QSizePolicy)

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

# event type (slider) index -> section; presets are slider values, from the 154 logged roasts of this machine
DEFAULT_SECTIONS: Final[list[dict[str, Any]]] = [
    {'slider': 0, 'name': 'Fan', 'presets': [20, 30, 50, 60, 80, 120], 'on': 'Fan On', 'off': 'Fan Off'},
    {'slider': 1, 'name': 'Drum', 'presets': [20, 25, 30, 50], 'on': 'Drum On', 'off': 'Drum Off'},
]
DEFAULT_STEP: Final[int] = 5
COMMIT_DELAY_MS: Final[int] = 700 # -/+ taps within this time are combined into one command and one event
TASKBAR_PX: Final[int] = 48 # height of the Windows 11 taskbar that an auto-hidden taskbar shows over the window
PRESET_VERSION: Final[int] = 1 # bump to re-apply the display preset once on existing profiles

_instance: 'TouchControls|None' = None


def px_per_mm(widget:QWidget) -> float:
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is not None and screen.physicalSize().width() > 0:
        return screen.geometry().width() / screen.physicalSize().width()
    return 96 / 25.4


class SliderSection(QFrame):
    """Controls for one event slider: setpoint and actual value, -/+ step buttons, presets, run/stop."""

    def __init__(self, aw:'ApplicationWindow', config:dict[str, Any], step:int, mm:float) -> None:
        super().__init__()
        self.aw = aw
        self.n:int = int(config['slider'])
        self.name:str = str(config['name'])
        self.step = step
        self.pending:int|None = None # target shown while -/+ taps are being collected
        self.commit_timer = QTimer(self)
        self.commit_timer.setSingleShot(True)
        self.commit_timer.timeout.connect(self.commit)
        key = self.name.lower()
        self.setObjectName(f'touch_{key}')

        color = self.event_color()
        button_h = int(round(10 * mm)) # at least 10 mm tall for reliable finger taps
        self.setStyleSheet(f"""
            QFrame#touch_{key} {{ border: 1px solid rgba(128,128,128,0.35); border-radius: 8px; }}
            QLabel#touch_{key}_title {{ font-size: {int(button_h * 0.42)}px; font-weight: bold; color: {color}; }}
            QLabel#touch_{key}_value {{ font-size: {int(button_h * 1.0)}px; font-weight: bold; }}
            QLabel#touch_{key}_actual {{ font-size: {int(button_h * 0.32)}px; color: rgba(160,160,160,1); }}
            QPushButton {{ min-height: {button_h}px; font-size: {int(button_h * 0.36)}px; border-radius: 6px;
                border: 1px solid rgba(128,128,128,0.5); background: rgba(128,128,128,0.15); }}
            QPushButton:pressed {{ background: rgba(128,128,128,0.45); }}
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
            presets.addWidget(b, i // 3, i % 3)
            self.preset_buttons[int(v)] = b

        run_row = QHBoxLayout()
        for state, label in (('on', config.get('on')), ('off', config.get('off'))):
            if label:
                b = QPushButton('Run' if state == 'on' else 'Stop')
                b.setObjectName(f'touch_{key}_{state}')
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.clicked.connect(lambda _=False, text=label: self.click_extra_button(text))
                run_row.addWidget(b)

        layout = QVBoxLayout(self)
        layout.setSpacing(int(round(1.5 * mm)))
        layout.setContentsMargins(*([int(round(2 * mm))] * 4))
        layout.addWidget(self.title)
        layout.addLayout(value_row)
        layout.addWidget(self.actual)
        layout.addLayout(presets)
        layout.addLayout(run_row)

    def event_color(self) -> str:
        try:
            return str(self.aw.qmc.EvalueColor[self.n])
        except Exception: # pylint: disable=broad-except
            return '#2b7bb9'

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
        try:
            self.aw.moveslider(self.n, v)
            self.aw.sliderReleased(self.n, force=True, updateLCD=True) # sends the slider's command and records the event
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)
        self.refresh()

    def click_extra_button(self, label:str) -> None:
        for b in self.aw.buttonlist:
            if b.text().replace('\n', ' ').strip() == label:
                b.click() # runs the custom button's own action
                return
        self.aw.sendmessage(QApplication.translate('Message', 'No button "{0}" found').format(label))

    def actual_value(self) -> float|None:
        try:
            if self.aw.eventquantifieractive[self.n]:
                temp, _ = self.aw.quantifier2tempandtime(self.n)
                if temp is not None and len(temp) > 0 and temp[-1] not in (None, -1):
                    return float(temp[-1])
        except Exception: # pylint: disable=broad-except
            pass
        return None

    def refresh(self) -> None:
        shown = self.pending if self.pending is not None else self.current()
        self.value.setText(f'{shown}' + (' …' if self.pending is not None else ''))
        actual = self.actual_value()
        self.actual.setText(f'actual {actual:.1f}' if actual is not None and self.aw.qmc.flagon else '')
        for v, b in self.preset_buttons.items():
            active = 'true' if v == shown else 'false'
            if b.property('active') != active:
                b.setProperty('active', active)
                style = b.style()
                if style is not None:
                    style.unpolish(b)
                    style.polish(b)


class TouchControls:
    def __init__(self, aw:'ApplicationWindow') -> None:
        self.aw = aw
        settings = QSettings()
        settings.beginGroup('TouchControls')
        self.enabled = bool(settings.value('enabled', True, type=bool))
        step = int(settings.value('step', DEFAULT_STEP, type=int))
        safe_margin = int(settings.value('bottomSafeMargin', -1, type=int)) # -1: automatic
        preset_version = int(settings.value('presetVersion', 0, type=int))
        settings.endGroup()

        mm = px_per_mm(aw)
        self.panel = QFrame()
        self.panel.setObjectName('touchPanel')
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(int(mm), int(mm), int(mm), int(mm))
        self.sections = [SliderSection(aw, config, step, mm) for config in DEFAULT_SECTIONS
            if config['slider'] < len(aw.eventslidervisibilities) and aw.eventslidervisibilities[config['slider']]]
        for s in self.sections:
            layout.addWidget(s)
        layout.addStretch(1) # nothing important at the bottom edge
        self.panel.setMinimumWidth(int(round(52 * mm)))
        self.panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        # part of the main layout, right of the LCDs (a dock widget would be placed by the window state restored before)
        aw.midlayout.addWidget(self.panel)
        self.panel.setVisible(self.enabled and bool(self.sections))

        self.action = QAction(QApplication.translate('Menu', 'Touch Controls'), aw)
        self.action.setCheckable(True)
        self.action.setChecked(self.panel.isVisible())
        self.action.triggered.connect(self.toggle)
        self.preset_action = QAction(QApplication.translate('Menu', 'Roast Screen Display Preset'), aw)
        self.preset_action.triggered.connect(lambda _=False: self.apply_display_preset())
        if aw.viewMenu is not None:
            aw.viewMenu.addSeparator()
            aw.viewMenu.addAction(self.action)
            aw.viewMenu.addAction(self.preset_action)

        self.apply_bottom_safe_margin(safe_margin)
        if preset_version < PRESET_VERSION:
            self.apply_display_preset()
            settings = QSettings()
            settings.setValue('TouchControls/presetVersion', PRESET_VERSION)

        self.timer = QTimer(aw)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def refresh(self) -> None:
        if self.panel.isVisible():
            for s in self.sections:
                s.refresh()

    def toggle(self, checked:bool = False) -> None:
        self.panel.setVisible(checked and bool(self.sections))
        QSettings().setValue('TouchControls/enabled', checked)

    # keeps the bottom rows of the main window out of the strip an auto-hidden taskbar slides over
    def apply_bottom_safe_margin(self, margin:int) -> None:
        if margin < 0:
            screen = self.aw.screen()
            if screen is None:
                return
            reserved = screen.geometry().bottom() - screen.availableGeometry().bottom() # space a visible taskbar already takes
            margin = max(0, TASKBAR_PX - reserved)
        layout = self.aw.main_widget.layout()
        if layout is not None:
            m = layout.contentsMargins()
            layout.setContentsMargins(m.left(), m.top(), m.right(), margin)

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
