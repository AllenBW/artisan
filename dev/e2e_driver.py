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
from PyQt6.QtGui import QMouseEvent, QPointingDevice, QInputDevice # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget, QSlider, QStyle, QStyleOptionSlider # noqa: E402
from pymodbus.client import ModbusTcpClient # noqa: E402

# roast time (s) of the scripted actions, matching the simulator's curve (DRY 329 s, FCs 540 s, DROP 680 s)
DRY_AT, FCS_AT, DROP_AT = 329, 540, 680


class Driver:
    def __init__(self, speed: float, port: int, out: Path) -> None:
        self.speed = speed
        self.sim = ModbusTcpClient('127.0.0.1', port=port)
        self.out = out.open('a', encoding='utf-8', buffering=1)
        self.aw: Any = None
        self.touch = QPointingDevice('e2e touchscreen', 4242, QInputDevice.DeviceType.TouchScreen,
            QPointingDevice.PointerType.Finger, QInputDevice.Capability.Position, 10, 0)
        self.mouse = QPointingDevice.primaryPointingDevice()
        self.steps: list[tuple[str, Callable[[], bool]]] = []
        self.step_started = 0.0
        self.charged_at: float | None = None
        self.failures: list[str] = []

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
        for _ in range(600):
            if (direction == -1 and slider.value() >= value) or (direction == 1 and slider.value() <= value):
                break
            pos.setY(pos.y() + direction)
            self.send(slider, QEvent.Type.MouseMove, pos, device, Qt.MouseButton.LeftButton)
        self.send(slider, QEvent.Type.MouseButtonRelease, pos, device, Qt.MouseButton.NoButton)
        self.record('drag_end', target=name, value=slider.value(), goal=value)

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

        self.steps = [
            ('settle', wait(4)),
            ('ON', once(lambda: self.tap(aw.buttonONOFF, 'ON'))),
            ('sampling', wait(5)),
            ('START', once(lambda: self.tap(aw.buttonSTARTSTOP, 'START', touch=False))),
            ('recording', wait(6)),
            ('fan on', once(lambda: self.tap(self.extra_button('Fan On'), 'Fan On'))),
            ('drum on', once(lambda: self.tap(self.extra_button('Drum On'), 'Drum On', touch=False))),
            ('fan 30', once(lambda: self.drag(aw.slider1, 'fan slider', 30))),
            ('drum 50', once(lambda: self.drag(aw.slider2, 'drum slider', 50))),
            ('preheat', wait(5)),
            ('charge', once(charge)),
            ('wait charge', ensure_charge),
            ('to TP+60', until_roast(130)),
            ('fan 20', once(lambda: self.drag(aw.slider1, 'fan slider', 20))),
            ('to DRY', until_roast(DRY_AT)),
            ('DRY', once(lambda: self.tap(aw.buttonDRY, 'DRY'))),
            ('fan 120', once(lambda: self.drag(aw.slider1, 'fan slider', 120))),
            ('heat 190', once(lambda: self.drag(aw.slider4, 'heat slider', 190, touch=False))),
            ('to mid', until_roast(450)),
            ('fan 60', once(lambda: self.drag(aw.slider1, 'fan slider', 60))),
            ('to FCs', until_roast(FCS_AT)),
            ('FCs', once(lambda: self.tap(aw.buttonFCs, 'FCs'))),
            ('fan 120 again', once(lambda: self.drag(aw.slider1, 'fan slider', 120))),
            ('to DROP', until_roast(DROP_AT)),
            ('drop', once(drop)),
            ('wait drop', ensure_drop),
            ('cooling', wait(6)),
            ('dialogs', once(dismiss_dialogs)),
            ('drum off', once(lambda: self.tap(self.extra_button('Drum Off'), 'Drum Off'))),
            ('fan off', once(lambda: self.tap(self.extra_button('Fan Off'), 'Fan Off', touch=False))),
            ('settle off', wait(3)),
            ('OFF', once(lambda: self.tap(aw.buttonONOFF, 'OFF'))),
            ('autosave', wait(5)),
            ('dialogs again', once(dismiss_dialogs)),
            ('finish', once(finish)),
        ]

    def tick(self) -> None:
        try:
            if self.aw is None:
                for w in QApplication.topLevelWidgets():
                    if type(w).__name__ == 'ApplicationWindow' and w.isVisible():
                        self.aw = w
                        assert self.sim.connect(), 'simulator not reachable'
                        self.record('app_ready', settings=QApplication.applicationName())
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
    args = ap.parse_args()
    session = Path(os.environ.get('ARTISAN_SESSION_DIR', Path.home() / 'artisan-sessions'))
    session.mkdir(parents=True, exist_ok=True)

    from artisanlib import main as artisan_main # creates the QApplication
    driver = Driver(args.speed, args.port, session / f'driver-{datetime.datetime.now(datetime.UTC).astimezone():%Y%m%d-%H%M%S}.jsonl')
    timer = QTimer()
    timer.timeout.connect(driver.tick)
    timer.start(100)
    artisan_main.main()


if __name__ == '__main__':
    main()
