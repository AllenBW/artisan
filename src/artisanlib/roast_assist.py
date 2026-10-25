#
# ABOUT
# Roast assistance for the touch panel:
#  - a guide that says where the roast is and what comes next, with targets from matched past roasts
#    (charge readiness, phase times against the background roast, first crack ETA, live development time and
#    ratio against the drop window, cool-down to a safe ET)
#  - toggles to log the heating elements (left, center, right) as events
#  - a one-tap charge preset for fan and drum
#  - a roast setup dialog: name, bean, green weight and a background roast matched by bean and batch size
#  - a weigh-out dialog that subtracts the stored weight of the cooling container
#  - the tasting and grind-setting log (artisanlib/tasting_log.py)
#
# Configuration (QSettings group 'RoastAssist'): chargeET, chargeFan, chargeDrum, dtrMin, dtrMax, safeET, tare,
# elements

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
import bisect
import logging
import threading
from typing import Final, Any, TYPE_CHECKING
from collections.abc import Callable

from PyQt6.QtCore import Qt, QObject, QSettings, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QDialog, QComboBox, QLineEdit, QListWidget, QListWidgetItem)

from artisanlib.roast_history import RoastHistory, normalize_bean
from artisanlib.tasting_log import TastingLog, TastingDialog, default_path as tasting_log_path
from artisanlib.util import getDataDirectory

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

ELEMENTS: Final[list[tuple[str, str]]] = [('L', 'Left'), ('C', 'Center'), ('R', 'Right')]
ELEMENTS_EVENT_TYPE: Final[int] = 2 # the event type logged for the heating elements (Artisan's unused 'Damper')
FC_BT_DEFAULT: Final[float] = 188.0 # BT at first crack when no history is available
WEIGHT_PRESETS: Final[list[int]] = [1200, 1400, 1800, 2000]


def mmss(seconds:float|None) -> str:
    if seconds is None:
        return '--:--'
    s = max(0, int(round(seconds)))
    return f'{s // 60}:{s % 60:02d}'


def last_value(values:list[Any]) -> float|None:
    for v in reversed(values[-5:]):
        if isinstance(v, (int, float)) and v != -1:
            return float(v)
    return None


class RoastAssist(QObject): # pyright: ignore[reportGeneralTypeIssues]
    """Guide, element toggles, charge preset, roast setup, weigh-out and the tasting log for the touch panel."""

    tastingAdded = pyqtSignal(object) # a tasting added from a worker thread (Claude), handled on the GUI thread

    def __init__(self, aw:'ApplicationWindow', mm:float, set_fan:Callable[[int], None]|None, set_drum:Callable[[int], None]|None) -> None:
        super().__init__()
        self.tastingAdded.connect(self.tasting_saved)
        self.aw = aw
        self.mm = mm
        self.set_fan = set_fan
        self.set_drum = set_drum
        s = QSettings()
        s.beginGroup('RoastAssist')
        self.charge_et = float(s.value('chargeET', 180.0, type=float))
        self.charge_fan = int(s.value('chargeFan', 120, type=int))
        self.charge_drum = int(s.value('chargeDrum', 50, type=int))
        self.dtr_min = float(s.value('dtrMin', 20.0, type=float))
        self.dtr_max = float(s.value('dtrMax', 25.0, type=float))
        self.safe_et = float(s.value('safeET', 100.0, type=float))
        self.tare = float(s.value('tare', 0.0, type=float))
        stored = s.value('elements', None)
        # folder of past roasts to match against; defaults to the profile folder
        history_folder = str(s.value('historyFolder', '', type=str) or getattr(aw, 'userprofilepath', '') or '')
        s.endGroup()
        self.elements:list[bool] = [str(v).lower() in {'true', '1'} for v in stored] if isinstance(stored, list) and len(stored) == 3 else [True, True, True]
        self.charge_recorded = False # element state recorded at CHARGE for the current roast
        self.cooldown_announced = False
        self.last_flagstart = False

        # roast history (indexed in the background; the first scan of a large folder takes a few seconds)
        data_dir = getDataDirectory()
        cache = os.path.join(data_dir, 'roast-index.json') if data_dir else None
        self.history = RoastHistory(history_folder, cache)
        self.history.load_cache()
        threading.Thread(target=self.history.refresh, daemon=True, name='roast-history').start()
        self.tastings = TastingLog(tasting_log_path(history_folder))
        self.tasting_listeners:list[Callable[[dict[str, Any]], None]] = [] # eg. the roast archive

        if len(aw.qmc.etypes) > ELEMENTS_EVENT_TYPE and aw.qmc.etypes[ELEMENTS_EVENT_TYPE] in {'Damper', ''}:
            aw.qmc.etypes[ELEMENTS_EVENT_TYPE] = 'Elements'

        h = int(round(9 * mm)) # touch target height
        self.guide = QLabel()
        self.guide.setObjectName('assist_guide')
        self.guide.setWordWrap(True)
        self.guide.setTextFormat(Qt.TextFormat.RichText)
        self.guide.setMinimumHeight(int(round(16 * mm)))
        self.guide.setStyleSheet(f'QLabel#assist_guide {{ font-size: {int(h * 0.36)}px; padding: {int(mm)}px; border-radius: 8px;'
            ' border: 1px solid rgba(128,128,128,0.35); background: rgba(128,128,128,0.10); }')

        self.element_buttons:list[QPushButton] = []
        elements_row = QHBoxLayout()
        title = QLabel('ELEMENTS')
        title.setStyleSheet(f'font-size: {int(h * 0.3)}px; font-weight: bold;')
        elements_row.addWidget(title)
        for i, (short, name) in enumerate(ELEMENTS):
            b = QPushButton(short)
            b.setObjectName(f'assist_element_{short}')
            b.setToolTip(f'{name} element on/off')
            b.setCheckable(True)
            b.setChecked(self.elements[i])
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.toggled.connect(lambda on, idx=i: self.element_toggled(idx, on))
            elements_row.addWidget(b, 1)
            self.element_buttons.append(b)

        actions = QGridLayout()
        self.charge_button = QPushButton(f'Charge setup: fan {self.charge_fan} · drum {self.charge_drum}')
        self.charge_button.setObjectName('assist_charge')
        self.charge_button.clicked.connect(self.charge_preset)
        self.setup_button = QPushButton('Roast setup')
        self.setup_button.setObjectName('assist_setup')
        self.setup_button.clicked.connect(self.open_setup)
        self.weigh_button = QPushButton('Weigh out')
        self.weigh_button.setObjectName('assist_weigh')
        self.weigh_button.clicked.connect(self.open_weigh_out)
        self.tasting_button = QPushButton('Tasting log')
        self.tasting_button.setObjectName('assist_tasting')
        self.tasting_button.clicked.connect(self.open_tasting)
        for b in (self.charge_button, self.setup_button, self.weigh_button, self.tasting_button):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        actions.addWidget(self.charge_button, 0, 0, 1, 2)
        actions.addWidget(self.setup_button, 1, 0)
        actions.addWidget(self.weigh_button, 1, 1)
        actions.addWidget(self.tasting_button, 2, 0)
        self.actions = actions # the Claude panel adds its button at (2, 1)

        self.box = QFrame()
        self.box.setObjectName('assist_box')
        self.box.setStyleSheet(f"""
            QPushButton {{ min-height: {h}px; font-size: {int(h * 0.34)}px; border-radius: 6px;
                border: 1px solid rgba(128,128,128,0.5); background: rgba(128,128,128,0.15); }}
            QPushButton:pressed {{ background: rgba(128,128,128,0.45); }}
            QPushButton[objectName^="assist_element_"]:checked {{ background: rgba(224,90,60,0.9); color: white; border-color: rgba(224,90,60,1); }}
            """)
        layout = QVBoxLayout(self.box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(int(round(1.5 * mm)))
        layout.addLayout(elements_row)
        layout.addLayout(actions)

    # ---- elements

    def element_state_text(self) -> str:
        on = [name for (short, name), state in zip(ELEMENTS, self.elements, strict=True) if state]
        return ('elements ' + '+'.join(n[0] for n in on) + ' on') if on else 'all elements off'

    def element_toggled(self, idx:int, on:bool) -> None:
        self.elements[idx] = on
        QSettings().setValue('RoastAssist/elements', [str(v).lower() for v in self.elements])
        name = ELEMENTS[idx][1]
        self.record_elements(f"{name} {'on' if on else 'off'} ({self.element_state_text()})")
        self.aw.sendmessage(f"{name} element {'on' if on else 'off'}")

    def record_elements(self, description:str) -> None:
        qmc = self.aw.qmc
        if not qmc.flagstart:
            return # recorded with the roast at CHARGE
        try:
            value = 10 * sum(self.elements) # 0..30 on the event scale: the number of elements on
            qmc.EventRecordAction(extraevent=1, eventtype=ELEMENTS_EVENT_TYPE,
                eventvalue=qmc.eventsExternal2InternalValue(value), eventdescription=description)
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)

    # ---- charge preset

    def charge_preset(self) -> None:
        if self.set_fan is not None:
            self.set_fan(self.charge_fan)
        if self.set_drum is not None:
            self.set_drum(self.charge_drum)

    # ---- readings

    def readings(self) -> tuple[float|None, float|None, float|None]:
        qmc = self.aw.qmc
        if qmc.flagstart:
            return last_value(qmc.temp1), last_value(qmc.temp2), last_value(qmc.delta2)
        if qmc.flagon:
            return last_value(qmc.on_temp1), last_value(qmc.on_temp2), last_value(getattr(qmc, 'on_delta2', []))
        return None, None, None

    def roast_time(self) -> float|None:
        qmc = self.aw.qmc
        try:
            if qmc.flagstart and qmc.timeindex[0] > -1 and qmc.timex:
                end = qmc.timex[qmc.timeindex[6]] if qmc.timeindex[6] > 0 else qmc.timeclock.elapsedMilli()
                return end - qmc.timex[qmc.timeindex[0]]
        except Exception: # pylint: disable=broad-except
            pass
        return None

    def mark_time(self, i:int) -> float|None:
        qmc = self.aw.qmc
        try:
            if qmc.timeindex[0] > -1 and qmc.timeindex[i] > 0:
                return qmc.timex[qmc.timeindex[i]] - qmc.timex[qmc.timeindex[0]]
        except Exception: # pylint: disable=broad-except
            pass
        return None

    def weight_in_g(self) -> float:
        try:
            w = self.aw.qmc.weight
            factor = {'g': 1.0, 'kg': 1000.0, 'lb': 453.592, 'oz': 28.3495}.get(str(w[2]).lower(), 1.0)
            return float(w[0] or 0) * factor
        except Exception: # pylint: disable=broad-except
            return 0.0

    # BT of the loaded background roast at the given time after its CHARGE
    def background_bt(self, t:float) -> float|None:
        qmc = self.aw.qmc
        try:
            if qmc.timeindexB[0] < 0 or not qmc.timeB:
                return None
            target = qmc.timeB[qmc.timeindexB[0]] + t
            i = bisect.bisect_left(qmc.timeB, target)
            if 0 < i < len(qmc.timeB) and qmc.temp2B[i] != -1:
                return float(qmc.temp2B[i])
        except Exception: # pylint: disable=broad-except
            pass
        return None

    def background_entry(self) -> dict[str, Any]|None:
        path = str(getattr(self.aw.qmc, 'backgroundpath', '') or '')
        if not path or not getattr(self.aw.qmc, 'background', False):
            return None
        norm = os.path.normcase(os.path.normpath(path))
        for r in self.history.roasts():
            if os.path.normcase(os.path.normpath(r['path'])) == norm:
                return r
        return None

    # ---- guide

    def refresh(self) -> None:
        qmc = self.aw.qmc
        # element state goes into the roast at CHARGE (toggles before it are only remembered)
        if qmc.flagstart != self.last_flagstart:
            self.last_flagstart = qmc.flagstart
            self.charge_recorded = False
            self.cooldown_announced = False
        if qmc.flagstart and not self.charge_recorded and qmc.timeindex[0] > -1:
            self.charge_recorded = True
            self.record_elements(f'at charge: {self.element_state_text()}')
        for b, state in zip(self.element_buttons, self.elements, strict=True):
            if b.isChecked() != state:
                b.blockSignals(True)
                b.setChecked(state)
                b.blockSignals(False)
        self.guide.setText(self.guide_text())

    def guide_text(self) -> str:
        qmc = self.aw.qmc
        et, bt, ror = self.readings()
        w = self.weight_in_g()
        bean = str(qmc.beans or '').strip()
        bg = self.background_entry()
        med = self.history.batch_size_medians(w) if w > 0 else {}
        ref = bg or {}
        ref_name = f"#{ref['name'].split(' ')[0]}" if bg else (f"{med.get('roasts')} roasts at {int(w)} g" if med.get('roasts') else '')
        def target(key:str) -> float|None:
            v = ref.get(key) if bg else None
            return v if isinstance(v, (int, float)) else med.get(key)
        setup = (f'{bean or "no bean set"} · {int(w)} g' if w > 0 else f'{bean or "no bean set"} · green weight not set')
        bg_note = ''
        if bg is None:
            bg_note = '<br><span style="color:#d9822b">No matching background: tap Roast setup.</span>'
        elif normalize_bean(bg['bean']) != normalize_bean(bean) or (w > 0 and bg['weight_in_g'] and abs(bg['weight_in_g'] - w) > 0.1 * w):
            bg_note = (f'<br><span style="color:#d9822b">Background {ref_name} is {bg["bean"]}, {int(bg["weight_in_g"] or 0)} g: '
                'not like this roast. Tap Roast setup.</span>')

        if not qmc.flagon:
            if qmc.timeindex[6] > 0 and qmc.timex:
                dev = self.mark_time(6)
                fc = self.mark_time(2)
                parts = [f'<b>Last roast</b>: drop {mmss(dev)}']
                if fc is not None and dev:
                    parts.append(f'development {mmss(dev - fc)} ({100 * (dev - fc) / dev:.0f} %)')
                if qmc.weight[1]:
                    parts.append(f'loss {100 * (qmc.weight[0] - qmc.weight[1]) / qmc.weight[0]:.1f} %' if qmc.weight[0] else '')
                return ' · '.join(p for p in parts if p)
            return f'<b>Off</b> · {setup}{bg_note}'

        t = self.roast_time()
        if not qmc.flagstart or qmc.timeindex[0] < 0:
            if et is None:
                return f'<b>Preheat</b> · {setup}'
            ready = et >= self.charge_et
            status = (f'<span style="color:#3fb950"><b>Ready to charge</b></span> (ET {et:.0f} ≥ {self.charge_et:.0f})' if ready
                else f'Charge at ET {self.charge_et:.0f} · now <b>{et:.0f}</b> ({self.charge_et - et:.0f} to go)')
            ce = med.get('charge_et')
            hint = f'<br>Your {int(w)} g roasts charged at ET {ce:.0f} (median).' if ce and w > 0 else ''
            return f'<b>Preheat</b> · {setup}<br>{status}{hint}{bg_note}'

        dry, fcs, drop = self.mark_time(1), self.mark_time(2), self.mark_time(6)
        ror_txt = f' · RoR {ror:.1f}' if ror is not None else ''
        if drop is not None:
            dev = drop - fcs if fcs is not None else None
            done = f'<b>Dropped</b> at {mmss(drop)}' + (f' · development {mmss(dev)} ({100 * dev / drop:.0f} %)' if dev is not None else ' · first crack not marked')
            if et is not None and et >= self.safe_et:
                cool = f'<br>Cool-down: ET <b>{et:.0f}</b> → {self.safe_et:.0f} before switching the roaster off'
            elif et is not None:
                cool = f'<br><span style="color:#3fb950"><b>ET {et:.0f} is below {self.safe_et:.0f}: the roaster can be switched off.</b></span>'
                if not self.cooldown_announced:
                    self.cooldown_announced = True
                    self.aw.sendmessage(f'ET below {self.safe_et:.0f} °C: the roaster can be switched off')
            else:
                cool = ''
            weigh = '' if qmc.weight[1] else '<br>Weigh out when cool: tap <b>Weigh out</b>.'
            return f'{done}{cool}{weigh}'
        if fcs is not None and t is not None:
            dev = t - fcs
            dtr = 100 * dev / t if t > 0 else 0
            lo, hi = fcs / (1 - self.dtr_min / 100), fcs / (1 - self.dtr_max / 100)
            color = '#3fb950' if self.dtr_min <= dtr <= self.dtr_max else ('#d9822b' if dtr > self.dtr_max else '')
            dtr_txt = f'<span style="color:{color}"><b>{dtr:.0f} %</b></span>' if color else f'<b>{dtr:.0f} %</b>'
            refdev = target('dev_time')
            return (f'<b>Development</b> {mmss(dev)} · {dtr_txt}{ror_txt}<br>'
                f'Drop window ({self.dtr_min:.0f}–{self.dtr_max:.0f} %): <b>{mmss(lo)}–{mmss(hi)}</b>'
                + (f' · {ref_name}: {mmss(refdev)}' if refdev else '') + bg_note)
        if dry is not None and t is not None:
            fc_bt = target('fcs_bt') or FC_BT_DEFAULT
            eta = ''
            if bt is not None and ror is not None and ror > 0.5 and fc_bt > bt:
                secs = (fc_bt - bt) / ror * 60
                style = 'color:#d9822b' if secs <= 60 else ''
                eta = f'<br>First crack (~{fc_bt:.0f} °C) expected in <span style="{style}"><b>~{mmss(secs)}</b></span> at the current RoR'
            ref_fc = target('fcs_time')
            return (f'<b>Maillard</b> {mmss(t - dry)} · roast {mmss(t)}{ror_txt}{eta}'
                + (f'<br>{ref_name}: first crack at {mmss(ref_fc)}' if ref_fc else '') + bg_note)
        if t is not None:
            ref_dry = target('dry_time')
            delta = ''
            bt_bg = self.background_bt(t) if bg else None
            if bt is not None and bt_bg is not None:
                delta = f' · BT {bt - bt_bg:+.0f} vs {ref_name}'
            return (f'<b>Drying</b> {mmss(t)}{ror_txt}{delta}'
                + (f'<br>{ref_name}: DRY END at {mmss(ref_dry)}' if ref_dry else '') + '<br>Tap DRY END when the beans turn yellow and smell sweet.' + bg_note)
        return f'<b>Recording</b> · {setup}{bg_note}'

    # ---- roast setup

    def open_setup(self) -> None:
        dlg = RoastSetupDialog(self.aw, self, self.mm)
        dlg.setObjectName('assist_setup_dialog')
        dlg.open()

    # ---- weigh out

    def open_weigh_out(self) -> None:
        dlg = WeighOutDialog(self.aw, self, self.mm)
        dlg.setObjectName('assist_weigh_dialog')
        dlg.open()

    # ---- tasting log

    def open_tasting(self) -> None:
        dlg = TastingDialog(self.aw, self, self.mm)
        dlg.setObjectName('assist_tasting_dialog')
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.open()

    def relay_tasting(self, entry:dict[str, Any]) -> None:
        self.tastingAdded.emit(entry)

    def tasting_saved(self, entry:dict[str, Any]) -> None:
        for listener in self.tasting_listeners:
            try:
                listener(entry)
            except Exception as e: # pylint: disable=broad-except
                _log.exception(e)


def touch_style(mm:float) -> str:
    h = int(round(9 * mm))
    return (f'QPushButton {{ min-height: {h}px; min-width: {int(h * 1.2)}px; font-size: {int(h * 0.36)}px; border-radius: 6px; }}'
        f' QLineEdit, QComboBox {{ min-height: {h}px; font-size: {int(h * 0.4)}px; }}'
        f' QLabel {{ font-size: {int(h * 0.32)}px; }} QListWidget {{ font-size: {int(h * 0.32)}px; }}'
        f' QListWidget::item {{ min-height: {h}px; }}')


class RoastSetupDialog(QDialog):
    """Name, bean, green weight and background roast, set before CHARGE."""

    def __init__(self, aw:'ApplicationWindow', assist:RoastAssist, mm:float) -> None:
        super().__init__(aw)
        self.aw = aw
        self.assist = assist
        self.setWindowTitle(QApplication.translate('Form Caption', 'Roast setup'))
        self.setStyleSheet(touch_style(mm))
        qmc = aw.qmc
        self.name = QLineEdit(str(qmc.title or ''))
        self.name.setObjectName('setup_name')
        self.bean = QComboBox()
        self.bean.setObjectName('setup_bean')
        self.bean.setEditable(True)
        self.bean.addItems(assist.history.beans())
        self.bean.setCurrentText(str(qmc.beans or ''))
        self.weight = QLineEdit(str(int(assist.weight_in_g())) if assist.weight_in_g() else '')
        self.weight.setObjectName('setup_weight')
        self.weight.setAlignment(Qt.AlignmentFlag.AlignCenter)
        minus = QPushButton('−50')
        plus = QPushButton('+50')
        minus.clicked.connect(lambda: self.step_weight(-50))
        plus.clicked.connect(lambda: self.step_weight(50))
        wrow = QHBoxLayout()
        wrow.addWidget(minus)
        wrow.addWidget(self.weight, 1)
        wrow.addWidget(plus)
        prow = QHBoxLayout()
        for w in WEIGHT_PRESETS:
            b = QPushButton(f'{w} g')
            b.setObjectName(f'setup_weight_{w}')
            b.clicked.connect(lambda _=False, v=w: self.set_weight(v))
            prow.addWidget(b)
        self.backgrounds = QListWidget()
        self.backgrounds.setObjectName('setup_backgrounds')
        self.bean.currentTextChanged.connect(lambda _: self.update_backgrounds())
        self.weight.textChanged.connect(lambda _: self.update_backgrounds())
        apply = QPushButton(QApplication.translate('Button', 'Apply'))
        apply.setObjectName('setup_apply')
        apply.setDefault(True)
        apply.clicked.connect(self.apply)
        cancel = QPushButton(QApplication.translate('Button', 'Cancel'))
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout = QVBoxLayout(self)
        for label, w2 in (('Roast name', self.name), ('Bean', self.bean)):
            layout.addWidget(QLabel(label))
            layout.addWidget(w2)
        layout.addWidget(QLabel('Green weight (g)'))
        layout.addLayout(wrow)
        layout.addLayout(prow)
        layout.addWidget(QLabel('Background roast (best match first: same bean and batch size)'))
        layout.addWidget(self.backgrounds, 1)
        layout.addLayout(buttons)
        self.resize(int(160 * mm), int(150 * mm))
        self.update_backgrounds()

    def set_weight(self, v:int) -> None:
        self.weight.setText(str(v)) # updates the background list through textChanged

    def step_weight(self, delta:int) -> None:
        try:
            v = int(float(self.weight.text() or 0))
        except ValueError:
            v = 0
        self.weight.setText(str(max(0, v + delta)))

    def weight_value(self) -> float:
        try:
            return float(self.weight.text().replace(',', '.'))
        except ValueError:
            return 0.0

    def update_backgrounds(self) -> None:
        self.backgrounds.clear()
        bean, w = self.bean.currentText(), self.weight_value()
        history = self.assist.history
        best = history.best_background(bean, w or None)
        rows = ([best] if best else []) + [r for r in history.similar(bean) if r is not best][:8]
        for r in rows:
            fc = f"FC {mmss(r['fcs_time'])}" if r.get('fcs_time') else 'FC not marked'
            text = f"#{r['name'].split(' ')[0]} {r['bean']} · {r['date']} · {int(r['weight_in_g'] or 0)} g · {fc} · drop {mmss(r.get('drop_time'))}"
            if r is best:
                text = '★ ' + text
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, r['path'])
            self.backgrounds.addItem(item)
        none = QListWidgetItem('No background')
        none.setData(Qt.ItemDataRole.UserRole, '')
        self.backgrounds.addItem(none)
        self.backgrounds.setCurrentRow(0)

    def apply(self) -> None:
        qmc = self.aw.qmc
        try:
            qmc.title = self.name.text().strip() or self.bean.currentText().strip() or qmc.title
            qmc.beans = self.bean.currentText().strip()
            w = self.weight_value()
            if w > 0:
                factor = {'g': 1.0, 'kg': 1000.0, 'lb': 453.592, 'oz': 28.3495}.get(str(qmc.weight[2]).lower(), 1.0)
                qmc.weight = (round(w / factor, 3), qmc.weight[1], qmc.weight[2]) # the weight is an immutable tuple
            item = self.backgrounds.currentItem()
            path = str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ''
            if path:
                self.aw.loadbackgroundRedraw(path, True)
            elif item is not None:
                self.aw.deleteBackground()
                qmc.redraw(recomputeAllDeltas=False)
            qmc.fileDirtySignal.emit()
            self.aw.sendmessage(f'Roast setup: {qmc.beans}, {int(w)} g' + (f', background {os.path.basename(path)}' if path else ''))
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)
        self.accept()


class WeighOutDialog(QDialog):
    """Roasted weight from the scale reading minus the stored weight of the cooling container."""

    def __init__(self, aw:'ApplicationWindow', assist:RoastAssist, mm:float) -> None:
        super().__init__(aw)
        self.aw = aw
        self.assist = assist
        self.setWindowTitle(QApplication.translate('Form Caption', 'Weigh out'))
        self.setStyleSheet(touch_style(mm))
        self.reading = QLineEdit()
        self.reading.setObjectName('weigh_reading')
        self.reading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reading.setPlaceholderText('scale reading in g')
        self.tare = QLineEdit(f'{assist.tare:g}')
        self.tare.setObjectName('weigh_tare')
        self.tare.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_label = QLabel()
        self.result_label.setObjectName('weigh_result')
        self.result_label.setWordWrap(True)
        self.target:QLineEdit = self.reading
        self.reading.textChanged.connect(lambda _: self.update_result())
        self.tare.textChanged.connect(lambda _: self.update_result())
        keypad = QGridLayout()
        for i, key in enumerate(['7', '8', '9', '4', '5', '6', '1', '2', '3', '.', '0', '⌫']):
            b = QPushButton(key)
            b.setObjectName(f'weigh_key_{key}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, k=key: self.key(k))
            keypad.addWidget(b, i // 3, i % 3)
        which = QHBoxLayout()
        for label, field in (('Scale reading', self.reading), ('Container (tare)', self.tare)):
            b = QPushButton(label)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, f=field: self.select(f))
            which.addWidget(b)
        apply = QPushButton(QApplication.translate('Button', 'Apply'))
        apply.setObjectName('weigh_apply')
        apply.clicked.connect(self.apply)
        cancel = QPushButton(QApplication.translate('Button', 'Cancel'))
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout = QVBoxLayout(self)
        w_in = assist.weight_in_g()
        layout.addWidget(QLabel(f'Green weight: {w_in:.0f} g' if w_in else 'Green weight not set (Roast setup)'))
        layout.addWidget(QLabel('Scale reading (g), cooling container included'))
        layout.addWidget(self.reading)
        layout.addWidget(QLabel('Cooling container (g), remembered'))
        layout.addWidget(self.tare)
        layout.addLayout(which)
        layout.addLayout(keypad)
        layout.addWidget(self.result_label)
        layout.addLayout(buttons)
        self.update_result()

    def select(self, field:QLineEdit) -> None:
        self.target = field
        field.setFocus()

    def key(self, k:str) -> None:
        text = self.target.text()
        if k == '⌫':
            self.target.setText(text[:-1])
        elif k != '.' or '.' not in text:
            self.target.setText(text + k)

    @staticmethod
    def number(field:QLineEdit) -> float|None:
        try:
            return float(field.text().replace(',', '.'))
        except ValueError:
            return None

    def weight_out(self) -> float|None:
        reading, tare = self.number(self.reading), self.number(self.tare) or 0.0
        if reading is None:
            return None
        return reading - tare

    def update_result(self) -> None:
        out = self.weight_out()
        w_in = self.assist.weight_in_g()
        if out is None:
            self.result_label.setText('Enter the scale reading.')
            return
        text = f'Weight out: <b>{out:.0f} g</b>'
        if w_in > 0:
            loss = 100 * (w_in - out) / w_in
            ok = 8 <= loss <= 25
            color = '#3fb950' if ok else '#e05a5a'
            text += f' · loss <span style="color:{color}"><b>{loss:.1f} %</b></span>'
            if not ok:
                text += '<br><span style="color:#e05a5a">Outside 8–25 %: check the reading and the container weight.</span>'
        self.result_label.setText(text)

    def apply(self) -> None:
        out = self.weight_out()
        if out is None or out <= 0:
            self.result_label.setText('Enter a scale reading larger than the container weight.')
            return
        qmc = self.aw.qmc
        tare = self.number(self.tare) or 0.0
        self.assist.tare = tare
        QSettings().setValue('RoastAssist/tare', tare)
        factor = {'g': 1.0, 'kg': 1000.0, 'lb': 453.592, 'oz': 28.3495}.get(str(qmc.weight[2]).lower(), 1.0)
        qmc.weight = (qmc.weight[0], round(out / factor, 3), qmc.weight[2]) # the weight is an immutable tuple
        qmc.fileDirtySignal.emit()
        self.aw.sendmessage(f'Weight out {out:.0f} g')
        self.accept()
