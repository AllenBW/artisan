"""Runs Artisan from source and operates it like a person would during a roast, against the FZ94
simulator (dev/fz94_sim.py), to test the complete capture pipeline end to end.

Started by dev/e2e-test.ps1, which prepares an isolated settings profile (E2E) pointing MODBUS at the simulator.
Taps and slider drags are delivered as real Qt mouse events, some tagged as coming from a touchscreen (as Qt
delivers touch on these widgets) and some from a mouse. Every scripted action is written to driver-<stamp>.jsonl
as ground truth for dev/e2e_verify.py.

Usage (from the repository root, environment prepared by e2e-test.ps1):
    python dev/e2e_driver.py --speed 4 --port 5020
"""

import argparse
import datetime
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))
os.chdir(REPO / 'src')

from PyQt6.QtCore import Qt, QPoint, QPointF, QTimer, QEvent # noqa: E402
from PyQt6.QtGui import QMouseEvent, QPointingDevice, QPointingDeviceUniqueId, QInputDevice # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget, QSlider, QStyle, QStyleOptionSlider # noqa: E402
from pymodbus.client import ModbusTcpClient # noqa: E402

# roast time (s) of the scripted actions, matching the simulator's curve (DRY 329 s, FCs 540 s, DROP 680 s)
DRY_AT, FCS_AT, DROP_AT = 329, 540, 680


class Driver:
    def __init__(self, speed: float, port: int, out: Path, maximize: bool = False, faults: str = '') -> None:
        self.speed = speed
        self.maximize = maximize
        # faults injected by the simulator, eg. 'drop=25:7;reject=22:8193:250'
        self.faults = faults
        self.rejects = {tuple(int(x) for x in f.split('=', 1)[1].split(':')) for f in faults.split(';') if f.startswith('reject=')}
        self.sim = ModbusTcpClient('127.0.0.1', port=port)
        self.out = out.open('a', encoding='utf-8', buffering=1)
        self.aw: Any = None
        # parented to the application so it is destroyed with it and never outlives Qt on exit
        self.touch = QPointingDevice('e2e touchscreen', 4242, QInputDevice.DeviceType.TouchScreen,
            QPointingDevice.PointerType.Finger, QInputDevice.Capability.Position, 10, 0, '', QPointingDeviceUniqueId(), QApplication.instance())
        self.mouse = QPointingDevice.primaryPointingDevice()
        self.steps: list[tuple[str, Callable[[], bool]]] = []
        self.step_started = 0.0
        self.charged_at: float | None = None
        self.failures: list[str] = []
        self.claude_replies = 0
        self.tasting_roast = ''

    def record(self, action: str, **data: Any) -> None:
        now = time.time()
        rec = {'ts': datetime.datetime.fromtimestamp(now, datetime.UTC).astimezone().isoformat(timespec='milliseconds'),
            'epoch': round(now, 3), 'action': action, **data}
        self.out.write(json.dumps(rec) + '\n')
        try:
            print(rec['ts'][11:23], action, data, flush=True)
        except (OSError, ValueError): # Artisan replaces stdout when it has no usable console
            pass

    # ---- input helpers (events go through QApplication.notify, like real input) ----

    def send(self, widget: QWidget, etype: QEvent.Type, pos: QPoint, device: QPointingDevice, buttons: Qt.MouseButton) -> None:
        button = Qt.MouseButton.LeftButton if etype != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
        gpos = widget.mapToGlobal(pos)
        ev = QMouseEvent(etype, QPointF(pos), QPointF(pos), QPointF(gpos), button, buttons, Qt.KeyboardModifier.NoModifier, device)
        QApplication.sendEvent(widget, ev)

    def tap(self, widget: QWidget, name: str, touch: bool = True) -> None:
        device = self.touch if touch else self.mouse
        center = widget.rect().center()
        self.record('tap', target=name, text=getattr(widget, 'text', lambda: '')().replace('\n', ' '), device='TouchScreen' if touch else 'Mouse')
        self.send(widget, QEvent.Type.MouseButtonPress, center, device, Qt.MouseButton.LeftButton)
        self.send(widget, QEvent.Type.MouseButtonRelease, center, device, Qt.MouseButton.NoButton)

    def drag(self, slider: QSlider, name: str, value: int, touch: bool = True) -> None:
        device = self.touch if touch else self.mouse
        opt = QStyleOptionSlider()
        slider.initStyleOption(opt)
        style = slider.style()
        assert style is not None
        handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, slider).center()
        before = slider.value()
        self.record('drag_start', target=name, value=before, goal=value, device='TouchScreen' if touch else 'Mouse')
        self.send(slider, QEvent.Type.MouseButtonPress, handle, device, Qt.MouseButton.LeftButton)
        pos = QPoint(handle)
        direction = -1 if value > before else 1 # vertical sliders: up increases the value
        for _ in range(slider.height() + slider.width() + 100): # one pixel per step, across the whole slider
            if (direction == -1 and slider.value() >= value) or (direction == 1 and slider.value() <= value):
                break
            pos.setY(pos.y() + direction)
            self.send(slider, QEvent.Type.MouseMove, pos, device, Qt.MouseButton.LeftButton)
        self.send(slider, QEvent.Type.MouseButtonRelease, pos, device, Qt.MouseButton.NoButton)
        self.record('drag_end', target=name, value=slider.value(), goal=value)
        if slider.value() != value:
            self.check(False, f'{name} reached {value} (stopped at {slider.value()})')

    def panel(self, name: str) -> QWidget:
        from PyQt6.QtWidgets import QPushButton
        w = self.aw.findChild(QPushButton, name)
        if w is None:
            raise LookupError(name)
        return w

    # taps panel buttons in a quick sequence (as a person would) and states the MODBUS writes that must follow
    def panel_taps(self, names: list[str], slider: str, value: int, writes: list[tuple[int, int, int]], within: float = 3.0,
            exactly: int | None = None, gap: float = 0.15) -> None:
        start = time.time()
        for i, name in enumerate(names):
            if i:
                QApplication.processEvents()
                time.sleep(gap)
            self.tap(self.panel(name), name)
        self.record('set', target=slider, value=value)
        for slave, register, v in writes:
            self.record('expect_write', slave=slave, register=register, value=v, after=round(start, 3), within=within, exactly=exactly)

    def extra_button(self, label: str) -> QWidget:
        for b in self.aw.buttonlist:
            if b.text().replace('\n', ' ') == label:
                return b
        raise LookupError(label)

    def sim_command(self, command: int, what: str) -> None:
        self.record('sim', command=what)
        self.sim.write_register(0, command, device_id=99)

    def roast_time(self) -> float:
        return 0 if self.charged_at is None else (time.time() - self.charged_at) * self.speed

    def check(self, ok: bool, what: str) -> None:
        self.record('check', what=what, ok=ok)
        if not ok:
            self.failures.append(what)

    # ---- the scripted roast ----

    def build(self) -> None:
        aw = self.aw
        qmc = aw.qmc
        wait = lambda seconds: (lambda: time.time() - self.step_started >= seconds) # noqa: E731
        def once(f: Callable[[], None]) -> Callable[[], bool]:
            return lambda: (f(), True)[1]
        def until_roast(t: float) -> Callable[[], bool]:
            return lambda: self.roast_time() >= t

        def charge() -> None:
            self.sim_command(1, 'charge')
            self.charged_at = time.time()
        def ensure_charge() -> bool:
            if qmc.timeindex[0] > -1:
                self.record('mark_seen', mark='CHARGE', auto=True)
                return True
            if time.time() - self.step_started > 20:
                self.record('mark_missing', mark='CHARGE', auto=True)
                self.tap(aw.buttonCHARGE, 'CHARGE')
                return True
            return False
        def drop() -> None:
            self.sim_command(2, 'drop')
        def ensure_drop() -> bool:
            if qmc.timeindex[6] > 0:
                self.record('mark_seen', mark='DROP', auto=True)
                return True
            if time.time() - self.step_started > 20:
                self.record('mark_missing', mark='DROP', auto=True)
                self.tap(aw.buttonDROP, 'DROP')
                return True
            return False
        def dismiss_dialogs() -> None:
            modal = QApplication.activeModalWidget()
            if modal is not None:
                self.record('dismiss_dialog', title=modal.windowTitle(), cls=type(modal).__name__)
                modal.close()
        def finish() -> None:
            self.check(qmc.timeindex[0] > -1, 'CHARGE marked')
            self.check(qmc.timeindex[1] > 0, 'DRY marked')
            self.check(qmc.timeindex[2] > 0, 'FCs marked')
            self.check(qmc.timeindex[6] > 0, 'DROP marked')
            self.record('quit', failures=self.failures)
            self.out.close()
            QTimer.singleShot(30000, lambda: os._exit(3)) # watchdog in case a dialog blocks quitting
            aw.close()

        # the display preset is offered once on a fresh profile; answer it like a person would
        def answer_preset_prompt() -> bool:
            from PyQt6.QtWidgets import QMessageBox
            box = aw.findChild(QMessageBox, 'touchPresetPrompt')
            if box is not None and box.isVisible():
                yes = box.button(QMessageBox.StandardButton.Yes)
                if yes is not None:
                    self.tap(yes, 'preset prompt Yes', touch=False)
                    return True
            if time.time() - self.step_started > 8:
                self.record('preset_prompt_missing')
                return True
            return False

        # after a fan/drum command: the panel shows it as not confirmed exactly when the simulator rejected it
        def check_panel_confirmation(section: str, slave: int, register: int, value: int) -> None:
            from PyQt6.QtWidgets import QLabel
            label = aw.findChild(QLabel, f'touch_{section}_actual')
            text = label.text() if label is not None else ''
            rejected = (slave, register, value) in self.rejects
            self.check(('not confirmed' in text) == rejected,
                f"panel shows {section} {value // 10} as {'not confirmed' if rejected else 'confirmed'} (shows '{text}')")

        # ---- roast assist (guide, roast setup, elements, charge preset, weigh-out)
        from PyQt6.QtWidgets import QDialog, QComboBox, QLineEdit, QListWidget, QLabel, QPushButton

        # every visible button and slider of a window: fully visible (not clipped by its containers or the window),
        # not overlapping another one, and the touch panel's buttons at least 8.5 mm tall
        def layout_check(phase: str, root: QWidget | None = None) -> None:
            from PyQt6.QtCore import QRect
            from PyQt6.QtWidgets import QAbstractButton
            from artisanlib.touch_controls import px_per_mm
            root = root or aw
            QApplication.processEvents() # pending layout and resize work first, as the screen would show it
            mm = px_per_mm(aw)
            def name(w: QWidget) -> str:
                text = getattr(w, 'text', lambda: '')().replace('\n', ' ').strip()
                return w.objectName() or (f'{type(w).__name__} {text!r}' if text else type(w).__name__)
            widgets = [w for w in [*root.findChildren(QAbstractButton), *root.findChildren(QSlider)]
                if w.isVisible() and w.window() is root and w.width() > 0 and w.height() > 0]
            rects: list[tuple[QWidget, QRect]] = []
            problems: list[str] = []
            for w in widgets:
                r = QRect(w.mapTo(root, QPoint(0, 0)), w.size())
                visible = QRect(r)
                p = w.parentWidget()
                while p is not None and p is not root:
                    visible = visible.intersected(QRect(p.mapTo(root, QPoint(0, 0)), p.size()))
                    p = p.parentWidget()
                visible = visible.intersected(root.rect())
                if visible != r:
                    problems.append(f'{name(w)} clipped ({visible.height()} of {r.height()} px tall visible)')
                if w.objectName().startswith(('touch_', 'assist_', 'tasting_', 'claude_', 'setup_', 'weigh_')) and r.height() < 8.5 * mm:
                    problems.append(f'{name(w)} only {r.height() / mm:.1f} mm tall')
                rects.append((w, r))
            for i, (a, ra) in enumerate(rects):
                for b, rb in rects[i + 1:]:
                    if a.isAncestorOf(b) or b.isAncestorOf(a):
                        continue
                    o = ra.intersected(rb)
                    if o.width() > 2 and o.height() > 2: # touching borders is fine
                        problems.append(f'{name(a)} overlaps {name(b)} ({o.width()}x{o.height()} px)')
            if root is not aw: # dialogs and panels: on the screen, out of the taskbar strip
                screen = root.screen()
                if screen is not None and not screen.availableGeometry().contains(root.frameGeometry()):
                    problems.append(f'window {root.frameGeometry().getRect()} outside the screen {screen.availableGeometry().getRect()}')
            self.record('layout', phase=phase, window=root.objectName() or type(root).__name__, widgets=len(rects), problems=problems[:20])
            self.check(not problems, f'layout {phase}: {len(rects)} buttons and sliders fully visible, none overlapping'
                + (f' ({len(problems)} problems: {"; ".join(problems[:4])})' if problems else ''))

        def guide_check(phase: str, *needles: str) -> None:
            layout_check(phase)
            label = aw.findChild(QLabel, 'assist_guide')
            text = label.text() if label is not None else ''
            plain = ' '.join(text.replace('<br>', ' ').split())
            missing = [n for n in needles if n not in plain]
            self.record('guide', phase=phase, text=plain[:300])
            self.check(not missing, f'guide during {phase} shows {needles}' + (f' (missing {missing})' if missing else ''))

        # the BEAN / IN / OUT tiles: each expected text in 'caption value' of a tile, shown in full (not cut short)
        def tiles_check(phase: str, *needles: str, attention: str | None = None) -> None:
            from artisanlib import touch_controls
            assist = touch_controls._instance.assist if touch_controls._instance is not None else None # noqa: SLF001
            if assist is None:
                self.check(False, f'tiles {phase}: touch panel present')
                return
            assist.refresh_tiles()
            tiles = (assist.bean_tile, assist.in_tile, assist.out_tile)
            shown = ' | '.join(f'{t.caption.text()} {t.full_value}' for t in tiles)
            missing = [n for n in needles if n not in shown]
            cut = [t.full_value for t in tiles if t.value.text() != t.full_value]
            flagged = [t.objectName() for t in tiles if t.property('attention')]
            self.record('tiles', phase=phase, shown=shown, cut=cut, attention=flagged)
            self.check(not missing and not cut, f'tiles {phase} show {needles} ({shown})'
                + (f' missing {missing}' if missing else '') + (f' cut short {cut}' if cut else ''))
            if attention is not None:
                self.check(flagged == ([attention] if attention else []), f'tiles {phase}: highlighted {flagged}, expected {attention or "none"}')

        def roast_setup() -> None:
            tiles_check('before setup', 'IN set', 'OUT weigh')
            self.tap(self.panel('assist_setup'), 'Roast setup')
            QApplication.processEvents()
            dlg = aw.findChild(QDialog, 'assist_setup_dialog')
            if dlg is None:
                self.check(False, 'roast setup dialog opens')
                return
            layout_check('roast setup dialog', dlg)
            bean = dlg.findChild(QComboBox, 'setup_bean')
            if bean is not None:
                bean.setCurrentText('India Natural')
                self.record('set_bean', value='India Natural')
            self.tap(dlg.findChild(QPushButton, 'setup_weight_1200'), 'setup 1200 g')
            QApplication.processEvents()
            backgrounds = dlg.findChild(QListWidget, 'setup_backgrounds')
            first = backgrounds.item(0) if backgrounds is not None and backgrounds.count() else None
            suggested = str(first.data(Qt.ItemDataRole.UserRole)) if first is not None else ''
            self.record('suggested_background', path=suggested, text=first.text() if first is not None else '')
            self.tap(dlg.findChild(QPushButton, 'setup_apply'), 'setup Apply')
            QApplication.processEvents()
            w = aw.qmc.weight
            self.check(str(aw.qmc.beans) == 'India Natural' and float(w[0]) == 1200.0, f'roast setup set bean and weight ({aw.qmc.beans}, {w[0]})')
            bg = str(getattr(aw.qmc, 'backgroundpath', '') or '')
            self.check(bool(suggested) and os.path.normcase(bg) == os.path.normcase(suggested),
                f'suggested background loaded ({os.path.basename(suggested)} -> {os.path.basename(bg)})')
            import re as _re
            grams = _re.search(r'(\d+) g', first.text()) if first is not None else None
            self.check(' India Natural ' in os.path.basename(suggested) and grams is not None and abs(int(grams.group(1)) - 1200) <= 120,
                f"suggested background is an India Natural of the same batch size ({grams.group(1) if grams else '?'} g)")
            tiles_check('after setup', 'BEAN · vs #', 'India Natural', 'IN 1200 g', 'OUT weigh', attention='')

        def weigh_out() -> None:
            self.tap(self.panel('assist_weigh'), 'Weigh out')
            QApplication.processEvents()
            dlg = aw.findChild(QDialog, 'assist_weigh_dialog')
            if dlg is None:
                self.check(False, 'weigh-out dialog opens')
                return
            layout_check('weigh-out dialog', dlg)
            tare, reading = dlg.findChild(QLineEdit, 'weigh_tare'), dlg.findChild(QLineEdit, 'weigh_reading')
            self.check(tare is not None and tare.text() == '1246', f"cooling container defaults to 1246 g ({tare.text() if tare else None})")
            if tare is not None and reading is not None:
                tare.setText('1246')
                reading.setFocus()
                for k in '2270':
                    self.tap(dlg.findChild(QPushButton, f'weigh_key_{k}'), f'key {k}', touch=True)
            result = dlg.findChild(QLabel, 'weigh_result')
            self.record('weigh_result', text=result.text() if result is not None else '')
            self.tap(dlg.findChild(QPushButton, 'weigh_apply'), 'weigh Apply')
            QApplication.processEvents()
            self.check(float(aw.qmc.weight[1]) == 1024.0, f'weight out = reading - container ({aw.qmc.weight[1]})')
            tiles_check('after weigh-out', 'OUT · 14.7 % 1024 g', 'IN 1200 g', attention='')

        # ---- Claude panel, against dev/fake_claude_api.py (ANTHROPIC_BASE_URL set by e2e-test.ps1)
        from PyQt6.QtWidgets import QTextBrowser

        def claude_panel() -> QWidget | None:
            return next((w for w in QApplication.topLevelWidgets() if w.objectName() == 'claude_panel' and w.isVisible()), None)

        def claude_open() -> None:
            self.tap(self.panel('assist_claude'), 'Ask Claude')
            QApplication.processEvents()
            panel = claude_panel()
            self.check(panel is not None, 'Claude panel opens')
            if panel is not None:
                layout_check('Claude panel', panel)

        def claude_ask(index: int) -> None:
            panel = claude_panel()
            if panel is None:
                return
            self.claude_replies = panel.findChild(QTextBrowser, 'claude_view').toPlainText().count('FAKE-CLAUDE reply')
            self.tap(panel.findChild(QPushButton, f'claude_quick_{index}'), f'Claude quick prompt {index}')

        def claude_reply() -> bool:
            panel = claude_panel()
            text = panel.findChild(QTextBrowser, 'claude_view').toPlainText() if panel is not None else ''
            if text.count('FAKE-CLAUDE reply') > self.claude_replies:
                line = next(l for l in reversed(text.splitlines()) if 'FAKE-CLAUDE reply' in l)
                self.record('claude_reply', text=line)
                self.check('BT is None' not in line, f'Claude panel reply used the live roast tool ({line.strip()})')
                return True
            if time.time() - self.step_started > 90: # the first reply includes loading the SDK
                self.check(False, f'Claude panel replied (shows {text[-200:]!r})')
                return True
            return False

        def claude_close() -> None:
            panel = claude_panel()
            if panel is not None:
                panel.close()

        # ---- tasting log (the profile points the log at the test folder)
        def tasting_dialog() -> QDialog | None:
            return next((d for d in aw.findChildren(QDialog, 'assist_tasting_dialog') if d.isVisible()), None)

        def tasting_entry() -> None:
            self.tap(self.panel('assist_tasting'), 'Tasting log')
            QApplication.processEvents()
            dlg = tasting_dialog()
            if dlg is None or not dlg.isVisible():
                self.check(False, 'tasting dialog opens')
                return
            layout_check('tasting dialog', dlg)
            def tap_name(name: str) -> None:
                self.tap(dlg.findChild(QPushButton, name), name)
            tap_name('tasting_method_espresso')
            for field, keys in (('grind', '12'), ('dose_g', '18'), ('yield_g', '36'), ('time_s', '28')):
                tap_name(f'tasting_select_{field}')
                for k in keys:
                    tap_name(f'tasting_key_{k}')
                if field == 'grind':
                    tap_name('tasting_step_+0.5')
            tap_name('tasting_score_4')
            tap_name('tasting_dev_right')
            tap_name('tasting_taste_sweet')
            roasts = dlg.findChild(QListWidget, 'tasting_roasts')
            item = roasts.currentItem() if roasts is not None else None
            self.tasting_roast = (item.data(Qt.ItemDataRole.UserRole) or {}).get('name', '') if item is not None else ''
            self.record('tasting_roast', roast=self.tasting_roast)
            tap_name('tasting_save')
            QApplication.processEvents()
            from artisanlib import touch_controls
            path = touch_controls._instance.assist.tastings.path if touch_controls._instance is not None else '' # noqa: SLF001
            rows = [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()] if path and Path(path).exists() else []
            last = rows[-1] if rows else {}
            self.record('tasting_saved', path=path, entry=last)
            self.check(last.get('roast') == self.tasting_roast and last.get('method') == 'Espresso' and last.get('grind') == 12.5
                and last.get('dose_g') == 18 and last.get('yield_g') == 36 and last.get('time_s') == 28 and last.get('score') == 4
                and last.get('development') == 'right' and last.get('descriptors') == ['Sweet'], f'tasting saved with the brew settings ({last})')

        def tasting_recall() -> None:
            self.tap(self.panel('assist_tasting'), 'Tasting log')
            QApplication.processEvents()
            dlg = tasting_dialog()
            if dlg is None:
                return
            self.tap(dlg.findChild(QPushButton, 'tasting_method_espresso'), 'tasting_method_espresso')
            last = dlg.findChild(QLabel, 'tasting_last')
            text = last.text() if last is not None else ''
            self.check('Last espresso' in text and '12.5' in text and '18→36 g' in text, f'tasting dialog recalls the last espresso ({text})')
            self.tap(dlg.findChild(QPushButton, 'tasting_use_last'), 'tasting_use_last')
            grind = dlg.findChild(QLineEdit, 'tasting_grind')
            self.check(grind is not None and grind.text() == '12.5', f"'Start from last' fills the grind ({grind.text() if grind else None})")
            self.tap(dlg.findChild(QPushButton, 'tasting_close'), 'tasting_close')

        def check_window() -> None:
            g = aw.frameGeometry()
            screen = aw.screen()
            if self.maximize and screen is not None:
                avail = screen.availableGeometry()
                self.check(g.height() >= 0.9 * avail.height() and g.width() >= 0.9 * avail.width(),
                    f'window fills the screen ({g.width()}x{g.height()} of {avail.width()}x{avail.height()})')

        self.steps = [
            ('preset prompt', answer_preset_prompt),
            ('settle', wait(2)),
            ('window', once(check_window)),
            ('history indexed', wait(6)), # first scan of the roast folder runs in the background
            ('roast setup', once(roast_setup)),
            ('layout off', once(lambda: layout_check('before ON'))),
            ('right element off', once(lambda: self.tap(self.panel('assist_element_R'), 'element R'))),
            ('ON', once(lambda: self.tap(aw.buttonONOFF, 'ON'))),
            ('sampling', wait(5)),
            ('guide preheat', once(lambda: guide_check('preheat', 'Preheat', 'ET'))),
            ('START', once(lambda: self.tap(aw.buttonSTARTSTOP, 'START', touch=False))),
            ('recording', wait(6)),
            ('fan on', once(lambda: self.tap(self.extra_button('Fan On'), 'Fan On'))),
            ('drum on', once(lambda: self.tap(self.extra_button('Drum On'), 'Drum On', touch=False))),
            ('fan 30', once(lambda: self.drag(aw.slider1, 'fan slider', 30))),
            ('drum 50', once(lambda: self.drag(aw.slider2, 'drum slider', 50))),
            ('charge preset', once(lambda: self.panel_taps(['assist_charge'], 'fan slider', 120, [(23, 8193, 1200)]))),
            ('preheat', wait(5)),
            ('charge', once(charge)),
            ('wait charge', ensure_charge),
            ('to TP+60', until_roast(130)),
            ('guide drying', once(lambda: guide_check('drying', 'Drying', 'DRY END at'))),
            ('fan 20', once(lambda: self.drag(aw.slider1, 'fan slider', 20))),
            ('claude open', once(claude_open)),
            ('claude ask 1', once(lambda: claude_ask(0))),
            ('claude reply 1', claude_reply),
            ('claude ask 2', once(lambda: claude_ask(1))), # a follow-up: the whole conversation goes with it
            ('claude reply 2', claude_reply),
            ('claude close', once(claude_close)),
            ('to DRY', until_roast(DRY_AT)),
            ('DRY', once(lambda: self.tap(aw.buttonDRY, 'DRY'))),
            ('after DRY', wait(1)), # a person looks at the screen before the next action
            ('panel fan 120', once(lambda: self.panel_taps(['touch_fan_preset_120'], 'fan slider', 120, [(23, 8193, 1200)]))),
            ('heat 190', once(lambda: self.drag(aw.slider4, 'heat slider', 190, touch=False))),
            ('panel drum 25', once(lambda: self.panel_taps(['touch_drum_preset_25'], 'drum slider', 25, [(22, 8193, 250)]))),
            ('drum 25 outcome', wait(1.5)),
            ('check drum 25', once(lambda: check_panel_confirmation('drum', 22, 8193, 250))),
            ('to mid', until_roast(450)),
            ('guide maillard', once(lambda: guide_check('Maillard', 'Maillard', 'First crack', 'expected in'))),
            ('right element on', once(lambda: self.tap(self.panel('assist_element_R'), 'element R'))),
            # four quick -5 taps: one command (1000) after the taps stop, not four
            ('panel fan -20', once(lambda: self.panel_taps(['touch_fan_minus'] * 4, 'fan slider', 100, [(23, 8193, 1000)], exactly=1))),
            ('panel settle', wait(2)),
            ('panel drum +5', once(lambda: self.panel_taps(['touch_drum_plus'], 'drum slider', 30, [(22, 8193, 300)], exactly=1))),
            ('to FCs', until_roast(FCS_AT)),
            ('FCs', once(lambda: self.tap(aw.buttonFCs, 'FCs'))),
            ('fan 120 again', once(lambda: self.drag(aw.slider1, 'fan slider', 120))),
            ('development', wait(4)),
            ('guide development', once(lambda: guide_check('development', 'Development', 'Drop window'))),
            ('to DROP', until_roast(DROP_AT)),
            ('drop', once(drop)),
            ('wait drop', ensure_drop),
            ('cooling', wait(6)),
            ('dialogs', once(dismiss_dialogs)),
            ('guide after drop', once(lambda: guide_check('cool-down', 'Dropped', 'development', 'Weigh out'))),
            ('tiles after drop', once(lambda: tiles_check('after drop', 'OUT weigh', attention='assist_weigh'))),
            ('weigh out', once(weigh_out)),
            ('panel drum stop', once(lambda: self.panel_taps(['touch_drum_off'], 'drum slider', 30, [(22, 8192, 1)]))),
            ('drum off', once(lambda: self.tap(self.extra_button('Drum Off'), 'Drum Off'))),
            ('fan off', once(lambda: self.tap(self.extra_button('Fan Off'), 'Fan Off', touch=False))),
            ('settle off', wait(3)),
            ('OFF', once(lambda: self.tap(aw.buttonONOFF, 'OFF'))),
            ('autosave', wait(5)),
            ('dialogs again', once(dismiss_dialogs)),
            ('layout after OFF', once(lambda: layout_check('after OFF'))),
            ('tasting', once(tasting_entry)),
            ('tasting recall', once(tasting_recall)),
            ('archive sync', wait(15)), # the roast archive commits and pushes after saves and tastings
            ('finish', once(finish)),
        ]

    def tick(self) -> None:
        try:
            if self.aw is None:
                for w in QApplication.topLevelWidgets():
                    if type(w).__name__ == 'ApplicationWindow' and w.isVisible():
                        self.aw = w
                        assert self.sim.connect(), 'simulator not reachable'
                        if self.maximize: # the layout used at the roaster: the full available screen area
                            screen = w.screen()
                            if screen is not None:
                                w.setWindowState(Qt.WindowState.WindowNoState)
                                w.setGeometry(screen.availableGeometry())
                            w.showMaximized()
                            QApplication.processEvents()
                        g = w.frameGeometry()
                        self.record('app_ready', settings=QApplication.applicationName(), speed=self.speed, maximized=self.maximize,
                            window=[g.x(), g.y(), g.width(), g.height()], faults=self.faults)
                        self.build()
                        self.step_started = time.time()
                return
            while self.steps:
                name, done = self.steps[0]
                if not done():
                    return
                self.record('step_done', step=name)
                self.steps.pop(0)
                self.step_started = time.time()
        except Exception as e: # pylint: disable=broad-except
            self.record('driver_error', error=repr(e))
            self.failures.append(repr(e))
            self.steps = [s for s in self.steps if s[0] in {'OFF', 'finish'}] # stop sampling and quit cleanly
            if not self.steps: # failed before the script was built
                QTimer.singleShot(1000, (self.aw.close if self.aw is not None else QApplication.quit))
                QTimer.singleShot(30000, lambda: os._exit(3))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--speed', type=float, default=4.0)
    ap.add_argument('--port', type=int, default=5020)
    ap.add_argument('--maximize', action='store_true', help='maximize the window, as used at the roaster')
    ap.add_argument('--faults', default='', help="faults injected by the simulator, eg. 'drop=25:7;reject=22:8193:250'")
    args = ap.parse_args()
    session = Path(os.environ.get('ARTISAN_SESSION_DIR', Path.home() / 'artisan-sessions'))
    session.mkdir(parents=True, exist_ok=True)

    from artisanlib import main as artisan_main # creates the QApplication
    driver = Driver(args.speed, args.port, session / f'driver-{datetime.datetime.now(datetime.UTC).astimezone():%Y%m%d-%H%M%S}.jsonl', args.maximize, args.faults)
    timer = QTimer()
    timer.timeout.connect(driver.tick)
    timer.start(100)
    artisan_main.main()


if __name__ == '__main__':
    main()
