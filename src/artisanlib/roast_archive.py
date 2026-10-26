#
# ABOUT
# Roast archive: after a roast is saved (and after a tasting), new and changed roast files and the tasting log are
# copied into a local git repository, committed and pushed (a private repository; dev/setup-roast-archive.ps1 creates
# it). The roast folder (eg. the shared Google Drive folder) stays where roasts are saved and shared; the repository
# keeps every version. Nothing is ever deleted from the repository.
#
# Configured with QSettings 'RoastArchive/repo' (the local clone; not inside a synced folder such as OneDrive or Google
# Drive, which can corrupt a .git folder), or the environment variable ARTISAN_ROAST_ARCHIVE. Off when neither is set.

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
import sys
import shutil
import logging
import threading
import subprocess
from pathlib import Path
from typing import Final, TYPE_CHECKING

from PyQt6.QtCore import QObject, QSettings, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import
    from artisanlib.roast_assist import RoastAssist # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

DEBOUNCE_MS: Final[int] = 8000 # saves come in bursts (autosave, then a save after the weigh-out)
ROASTS_DIR: Final[str] = 'roasts'
TASTING_FILE: Final[str] = 'tasting-log.jsonl'


def git_executable() -> str|None:
    found = shutil.which('git')
    if found:
        return found
    for candidate in (r'C:\Program Files\Git\cmd\git.exe', r'C:\Program Files (x86)\Git\cmd\git.exe'):
        if os.path.isfile(candidate):
            return candidate
    return None


class ArchiveRelay(QObject): # pyright: ignore[reportGeneralTypeIssues]
    message = pyqtSignal(str)


class RoastArchive:
    def __init__(self, aw:'ApplicationWindow', assist:'RoastAssist|None', repo:str) -> None:
        self.aw = aw
        self.assist = assist
        self.repo = repo
        self.git = git_executable()
        self.lock = threading.Lock()
        self.pending = False
        self.extra:set[str] = set() # saved roast files outside the roast folder (eg. a test folder)
        self.relay = ArchiveRelay()
        # sendmessage is a decorated slot with another signature: connecting it directly raises a TypeError
        self.relay.message.connect(lambda text: aw.sendmessage(text)) # noqa: PLW0108 # pylint: disable=unnecessary-lambda
        self.timer = QTimer(aw)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.sync)
        self.last_result = ''

    # ---- triggers (GUI thread)

    def saved(self) -> None:
        cur = str(getattr(self.aw, 'curFile', '') or '')
        if cur.lower().endswith('.alog') and os.path.isfile(cur):
            self.extra.add(cur)
        self.timer.start(DEBOUNCE_MS)

    def schedule(self, delay_ms:int = DEBOUNCE_MS) -> None:
        self.timer.start(delay_ms)

    def sync(self) -> None:
        if self.lock.locked():
            self.pending = True
            return
        threading.Thread(target=self.run, daemon=True, name='roast-archive').start()

    # ---- work (worker thread)

    def sources(self) -> tuple[list[str], str|None]:
        files:list[str] = []
        folder = self.assist.history.folder if self.assist is not None else ''
        if folder and os.path.isdir(folder):
            files.extend(str(p) for p in Path(folder).glob('*.alog'))
        files.extend(f for f in self.extra if os.path.isfile(f))
        tasting = self.assist.tastings.path if self.assist is not None else None
        return sorted(set(files)), (tasting if tasting and os.path.isfile(tasting) else None)

    def run_git(self, *args:str, timeout:float = 60) -> subprocess.CompletedProcess[str]:
        assert self.git is not None
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0 # type: ignore[attr-defined,unused-ignore]
        return subprocess.run([self.git, '-C', self.repo, *args], capture_output=True, text=True, timeout=timeout,
            creationflags=flags, check=False)

    def run(self) -> None:
        with self.lock:
            try:
                self.last_result = self.sync_once()
                if self.last_result:
                    self.relay.message.emit(f'Roast archive: {self.last_result}')
            except Exception as e: # pylint: disable=broad-except
                _log.exception(e)
                self.last_result = f'error: {e}'
                self.relay.message.emit(f'Roast archive not updated: {e}')
        if self.pending:
            self.pending = False
            self.run()

    def sync_once(self) -> str:
        if self.git is None:
            return 'git not found'
        if not os.path.isdir(os.path.join(self.repo, '.git')):
            return f'{self.repo} is not a git repository (run dev\\setup-roast-archive.ps1)'
        roasts, tasting = self.sources()
        dest = Path(self.repo) / ROASTS_DIR
        dest.mkdir(parents=True, exist_ok=True)
        added, changed = [], []
        for src in roasts:
            target = dest / os.path.basename(src)
            try:
                st = os.stat(src)
                if target.exists():
                    tt = target.stat()
                    if tt.st_size == st.st_size and int(tt.st_mtime) == int(st.st_mtime):
                        continue
                    changed.append(target.stem)
                else:
                    added.append(target.stem)
                shutil.copy2(src, target)
            except OSError as e:
                _log.debug('roast archive: %s %s', src, e)
        tasting_changed = False
        if tasting is not None:
            target = Path(self.repo) / TASTING_FILE
            st = os.stat(tasting)
            if not target.exists() or target.stat().st_size != st.st_size or int(target.stat().st_mtime) != int(st.st_mtime):
                shutil.copy2(tasting, target)
                tasting_changed = True
        self.run_git('add', '--', ROASTS_DIR, *( [TASTING_FILE] if (Path(self.repo) / TASTING_FILE).exists() else []))
        if self.run_git('diff', '--cached', '--quiet').returncode == 0:
            return self.push_if_ahead()
        parts = []
        if added:
            parts.append('add ' + ', '.join(added[:3]) + (f' and {len(added) - 3} more' if len(added) > 3 else ''))
        if changed:
            parts.append(f'update {len(changed)} roast' + ('s' if len(changed) != 1 else ''))
        if tasting_changed:
            parts.append('tastings')
        message = '; '.join(parts) or 'update'
        commit = self.run_git('commit', '-m', message[:200])
        if commit.returncode != 0:
            return f'commit failed: {(commit.stderr or commit.stdout).strip()[:200]}'
        pushed = self.push_if_ahead()
        return f'committed ({message})' + (f', {pushed}' if pushed else '')

    def push_if_ahead(self) -> str:
        remotes = self.run_git('remote').stdout.split()
        if not remotes:
            return ''
        status = self.run_git('status', '--porcelain=v2', '--branch').stdout
        ahead = next((line for line in status.splitlines() if line.startswith('# branch.ab ')), '')
        if ahead and ahead.split()[2] == '+0':
            return ''
        remote = 'origin' if 'origin' in remotes else remotes[0]
        push = self.run_git('push', '--quiet', '--set-upstream', remote, 'HEAD', timeout=120)
        if push.returncode != 0:
            _log.warning('roast archive push failed: %s', push.stderr.strip())
            return 'not pushed (kept locally, retried after the next save)'
        return 'pushed'


_instance:RoastArchive|None = None


def install(aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
    global _instance # pylint: disable=global-statement
    if _instance is not None or aw.app.artisanviewerMode:
        return
    repo = os.environ.get('ARTISAN_ROAST_ARCHIVE', '').strip() or str(QSettings().value('RoastArchive/repo', '', type=str) or '')
    if not repo:
        return
    _instance = RoastArchive(aw, assist, repo)
    archive = _instance
    aw.qmc.fileCleanSignal.connect(archive.saved) # emitted after every save (and after a load: nothing new then)
    if assist is not None:
        assist.tasting_listeners.append(lambda _entry: archive.schedule(2000))
    action = QAction(QApplication.translate('Menu', 'Sync roast archive now'), aw)
    action.triggered.connect(lambda: archive.schedule(0))
    from artisanlib.touch_controls import add_menu_action
    add_menu_action(aw, action)
    archive.schedule(30000) # catch up on roasts saved elsewhere, after the history index is up
