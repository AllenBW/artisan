#
# ABOUT
# In-app Claude panel: chat with Claude about the roast in progress, past roasts and tastings, with quick prompts for
# the touch screen. Uses the Claude API with the operator's own API key (stored in the Windows Credential Manager via
# keyring, or the ANTHROPIC_API_KEY environment variable) and the same read-only tools as the MCP server
# (artisanlib/roast_tools.py): Claude can read the roast and history and add notes and tastings, but cannot change
# fan, drum, heat or marks.
#
# Requests run on a worker thread with the SDK's tool runner (model claude-opus-5-5, adaptive thinking, explicit effort,
# server-side refusal fallbacks). Requires the 'anthropic' package; without it the panel says so.

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
import logging
import threading
from typing import Final, Any, TYPE_CHECKING, cast, override

from PyQt6.QtCore import Qt, QObject, QSettings, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (QApplication, QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLineEdit, QTextBrowser, QWidget)

from artisanlib.roast_tools import RoastTools, INSTRUCTIONS, DESCRIPTIONS

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import
    from artisanlib.roast_assist import RoastAssist # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

MODEL: Final[str] = 'claude-opus-5-5'
MAX_TOKENS: Final[int] = 16000
FALLBACK_BETA: Final[str] = 'server-side-fallback-2026-07-01' # for fallbacks='default'
KEYRING_SERVICE: Final[str] = 'artisan-claude'
KEYRING_USER: Final[str] = 'anthropic-api-key'

SYSTEM: Final[str] = INSTRUCTIONS + """

You are in a panel inside Artisan, on the roaster's touch screen. The operator wants to roast by numbers rather than by
feel, and to learn: say what to watch and why, with the numbers (BT, rate of rise, times, development ratio) that
back it. Keep answers short (a few lines during a roast), plain text or simple markdown lists, no tables. Look at the
data with the tools before answering; don't guess readings."""

# (button label, prompt, effort)
QUICK_PROMPTS: Final[list[tuple[str, str, str]]] = [
    ("How's it going?", 'How is the roast going? Compare with the background and matched roasts.', 'medium'),
    ('What next?', 'What should I watch for next, and when? Give me the numbers.', 'medium'),
    ('Debrief', 'Debrief the last roast: what went well, what to change next time, and why. Use the matched roasts and '
        'any tastings.', 'high'),
    ('Dial in', 'Help me dial in my grind for the most recent roast I tasted, from the tasting log: what to change and '
        'whether the roast itself is the problem.', 'high'),
]


def load_key() -> tuple[str, str]:
    """The API key and where it came from."""
    env = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if env:
        return env, 'ANTHROPIC_API_KEY'
    try:
        import keyring
        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if stored:
            return stored, 'Credential Manager'
    except Exception as e: # pylint: disable=broad-except
        _log.debug(e)
    return '', ''


class ChatRelay(QObject): # pyright: ignore[reportGeneralTypeIssues]
    """Carries the worker's results to the GUI thread."""
    reply = pyqtSignal(str)
    status = pyqtSignal(str)
    failed = pyqtSignal(str)
    done = pyqtSignal()


class ClaudeChat:
    """The conversation and its requests (run on a worker thread)."""

    def __init__(self, tools:RoastTools, relay:ChatRelay) -> None:
        self.tools = tools
        self.relay = relay
        self.messages:list[dict[str, Any]] = [] # mirrored history, sent in full each turn
        self.busy = False

    def tool_functions(self) -> list[Any]:
        from anthropic import beta_tool # pylint: disable=import-error
        t, d, relay = self.tools, DESCRIPTIONS, self.relay

        def out(value:Any) -> str:
            return json.dumps(value, ensure_ascii=False, default=str)

        @beta_tool(description=d['get_live_roast'])
        def get_live_roast() -> str:
            relay.status.emit('reading the live roast…')
            return out(t.live())

        @beta_tool(description=d['get_roast_curve'])
        def get_roast_curve(step_s:float = 10.0) -> str:
            """
            Args:
                step_s: Seconds between rows, at least 2.
            """
            relay.status.emit('reading the roast curve…')
            return out(t.curve(step_s))

        @beta_tool(description=d['list_roasts'])
        def list_roasts(bean:str = '', min_weight_g:float = 0.0, max_weight_g:float = 0.0, limit:int = 20) -> str:
            """
            Args:
                bean: Part of the bean name, case-insensitive; empty for all.
                min_weight_g: Smallest green weight in grams; 0 for no limit.
                max_weight_g: Largest green weight in grams; 0 for no limit.
                limit: How many roasts at most.
            """
            relay.status.emit('looking up past roasts…')
            return out(t.list_roasts(bean, min_weight_g, max_weight_g, limit))

        @beta_tool(description=d['get_roast'])
        def get_roast(name:str, step_s:float = 10.0) -> str:
            """
            Args:
                name: The roast's name or batch number.
                step_s: Seconds between curve rows, at least 2.
            """
            relay.status.emit(f'reading roast {name}…')
            return out(t.get_roast(name, step_s))

        @beta_tool(description=d['compare_with_history'])
        def compare_with_history() -> str:
            relay.status.emit('comparing with matched roasts…')
            return out(t.compare_with_history())

        @beta_tool(description=d['list_tastings'])
        def list_tastings(roast:str = '', bean:str = '', method:str = '', limit:int = 30) -> str:
            """
            Args:
                roast: A roast name or batch number; empty for all.
                bean: Part of the bean name; empty for all.
                method: A brew method; empty for all.
                limit: How many tastings at most.
            """
            relay.status.emit('reading the tasting log…')
            return out(t.list_tastings(roast, bean, method, limit))

        @beta_tool(description=d['add_tasting'])
        def add_tasting(roast:str, method:str, score:int = 0, development:str = '', grinder:str = '', grind:str = '',
                dose_g:float = 0.0, yield_g:float = 0.0, time_s:float = 0.0, descriptors:list[str]|None = None, notes:str = '') -> str:
            """
            Args:
                roast: The roast's name or batch number.
                method: The brew method.
                score: 1 to 5; 0 if not given.
                development: under, right or over; empty if not given.
                grinder: The grinder.
                grind: The grind setting as shown on the grinder.
                dose_g: Coffee dose in grams.
                yield_g: Beverage yield in grams.
                time_s: Brew or shot time in seconds.
                descriptors: Taste descriptors.
                notes: Anything else the operator said.
            """
            relay.status.emit('saving the tasting…')
            return out(t.add_tasting(roast, method, score, development, grinder, grind, dose_g, yield_g, time_s, descriptors, notes))

        @beta_tool(description=d['add_roast_note'])
        def add_roast_note(text:str) -> str:
            """
            Args:
                text: The note.
            """
            return t.add_roast_note(text)

        return [get_live_roast, get_roast_curve, list_roasts, get_roast, compare_with_history, list_tastings, add_tasting, add_roast_note]

    @staticmethod
    def echo_content(content:list[Any]) -> list[Any]:
        """The assistant content to send back next turn. After a mid-output fallback, the thinking and tool_use blocks
        before the last fallback block are left out."""
        types = [getattr(b, 'type', '') for b in content]
        if 'fallback' not in types:
            return list(content)
        boundary = len(types) - 1 - types[::-1].index('fallback')
        return [b for i, b in enumerate(content) if i >= boundary or types[i] not in {'thinking', 'redacted_thinking', 'tool_use'}]

    def send(self, text:str, effort:str, api_key:str) -> None:
        self.busy = True
        threading.Thread(target=self.run, args=(text, effort, api_key), daemon=True, name='claude-panel').start()

    def run(self, text:str, effort:str, api_key:str) -> None:
        start = len(self.messages)
        try:
            import anthropic # pylint: disable=import-error
        except ImportError:
            self.relay.failed.emit("The 'anthropic' package is not installed (run dev\\setup-dev.ps1).")
            self.busy = False
            self.relay.done.emit()
            return
        self.messages.append({'role': 'user', 'content': text})
        try:
            client = anthropic.Anthropic(api_key=api_key)
            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                tools=self.tool_functions(),
                messages=cast(Any, list(self.messages)), # plain dicts and SDK blocks, as the API takes them
                thinking={'type': 'adaptive'},
                output_config={'effort': cast(Any, effort)}, # low | medium | high | xhigh | max
                cache_control={'type': 'ephemeral'},
                betas=[FALLBACK_BETA],
                fallbacks='default',
                thread=False, # send the whole (mirrored) history each turn
                max_iterations=12,
            )
            self.relay.status.emit('thinking…')
            for message in runner:
                if message.stop_reason == 'refusal':
                    del self.messages[start:] # the refused turn is not kept
                    self.relay.failed.emit('Claude declined to answer that.')
                    break
                self.messages.append({'role': 'assistant', 'content': self.echo_content(message.content)})
                reply = '\n\n'.join(b.text for b in message.content if b.type == 'text' and b.text.strip())
                if reply:
                    self.relay.reply.emit(reply)
                if message.stop_reason == 'max_tokens':
                    if any(getattr(b, 'type', '') == 'tool_use' for b in message.content):
                        self.messages.pop() # an unfinished tool call cannot be sent back
                    self.relay.failed.emit('The answer was cut off (too long).')
                    break
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    self.messages.append(dict(tool_response))
                    self.relay.status.emit('thinking…')
        except anthropic.AuthenticationError:
            del self.messages[start:]
            self.relay.failed.emit('The API key was not accepted. Tap “API key” to change it.')
        except anthropic.PermissionDeniedError:
            del self.messages[start:]
            self.relay.failed.emit('This API key may not use this model.')
        except anthropic.RateLimitError:
            del self.messages[start:]
            self.relay.failed.emit('Rate limited by the Claude API: try again in a minute.')
        except anthropic.APIConnectionError:
            del self.messages[start:]
            self.relay.failed.emit('No connection to the Claude API: check the internet connection.')
        except anthropic.APIStatusError as e:
            del self.messages[start:]
            _log.error('Claude API error %s: %s', e.status_code, e.message)
            self.relay.failed.emit(f'Claude API error {e.status_code}: {e.message}')
        except Exception as e: # pylint: disable=broad-except
            del self.messages[start:]
            _log.exception(e)
            self.relay.failed.emit(f'Error: {e}')
        finally:
            self.busy = False
            self.relay.done.emit()


class ApiKeyDialog(QDialog):
    def __init__(self, parent:QWidget, mm:float) -> None:
        super().__init__(parent)
        from artisanlib.roast_assist import touch_style
        self.setWindowTitle('Claude API key')
        self.setStyleSheet(touch_style(mm))
        self.key = QLineEdit()
        self.key.setObjectName('claude_key')
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('sk-ant-…')
        info = QLabel('Create a key at console.anthropic.com (Settings → API keys). It is stored in the Windows '
            'Credential Manager on this computer, not in Artisan’s settings.')
        info.setWordWrap(True)
        self.message = QLabel()
        self.message.setWordWrap(True)
        save = QPushButton('Save')
        save.setObjectName('claude_key_save')
        save.clicked.connect(self.save)
        remove = QPushButton('Remove key')
        remove.setObjectName('claude_key_remove')
        remove.clicked.connect(self.remove)
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(remove)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.key)
        layout.addWidget(self.message)
        layout.addLayout(buttons)
        self.resize(int(140 * mm), int(60 * mm))

    def save(self) -> None:
        key = self.key.text().strip()
        if not key:
            self.message.setText('Paste the key first.')
            return
        try:
            import keyring
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        except Exception as e: # pylint: disable=broad-except
            self.message.setText(f'Could not store the key: {e}')
            return
        self.accept()

    def remove(self) -> None:
        try:
            import keyring
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception as e: # pylint: disable=broad-except
            _log.debug(e)
        self.accept()


class ClaudePanel(QDialog):
    """A non-modal chat window that stays above the main window."""

    def __init__(self, aw:'ApplicationWindow', assist:'RoastAssist|None', mm:float) -> None:
        super().__init__(aw, Qt.WindowType.Tool)
        from artisanlib.roast_assist import touch_style
        self.aw = aw
        self.mm = mm
        self.setObjectName('claude_panel')
        self.setWindowTitle('Claude')
        self.setModal(False)
        self.setStyleSheet(touch_style(mm))
        self.relay = ChatRelay()
        self.chat = ClaudeChat(RoastTools(aw, assist), self.relay)
        self.transcript:list[tuple[str, str]] = []

        self.status = QLabel()
        self.status.setObjectName('claude_status')
        self.status.setWordWrap(True)
        key_button = QPushButton('API key')
        key_button.setObjectName('claude_key_button')
        key_button.clicked.connect(self.edit_key)
        new_button = QPushButton('New chat')
        new_button.setObjectName('claude_new')
        new_button.clicked.connect(self.new_chat)
        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(new_button)
        top.addWidget(key_button)

        self.view = QTextBrowser()
        self.view.setObjectName('claude_view')
        self.view.setOpenExternalLinks(True)
        self.view.setStyleSheet(f'font-size: {int(9 * mm * 0.36)}px;')

        quick = QGridLayout()
        self.quick_buttons:list[QPushButton] = []
        for i, (label, prompt, effort) in enumerate(QUICK_PROMPTS):
            b = QPushButton(label)
            b.setObjectName(f'claude_quick_{i}')
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, p=prompt, e=effort, l=label: self.ask(p, e, l))
            quick.addWidget(b, i // 2, i % 2)
            self.quick_buttons.append(b)
        self.input = QLineEdit()
        self.input.setObjectName('claude_input')
        self.input.setPlaceholderText('Ask Claude about this roast…')
        self.input.returnPressed.connect(self.send_typed)
        self.send_button = QPushButton('Send')
        self.send_button.setObjectName('claude_send')
        self.send_button.clicked.connect(self.send_typed)
        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addLayout(quick)
        layout.addLayout(row)

        self.relay.reply.connect(lambda text: self.add('Claude', text))
        self.relay.status.connect(self.set_working)
        self.relay.failed.connect(lambda text: self.add('Note', text))
        self.relay.done.connect(self.on_done)

        geometry = QSettings().value('Assistant/panelGeometry')
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(int(120 * mm), int(150 * mm))
        self.update_status()
        self.show_transcript()

    @override
    def closeEvent(self, a0:QCloseEvent|None) -> None: # pylint: disable=invalid-name
        QSettings().setValue('Assistant/panelGeometry', self.saveGeometry())
        super().closeEvent(a0)

    def update_status(self) -> None:
        key, source = load_key()
        if self.chat.busy:
            return
        self.status.setText(f'Claude Opus 5.5 · key from {source}' if key else 'No API key yet: tap “API key”.')

    def set_working(self, text:str) -> None:
        self.status.setText(f'Claude is {text}')

    def edit_key(self) -> None:
        dlg = ApiKeyDialog(self, self.mm)
        dlg.finished.connect(lambda _: self.update_status())
        dlg.open()

    def new_chat(self) -> None:
        if self.chat.busy:
            return
        self.chat.messages.clear()
        self.transcript.clear()
        self.show_transcript()

    def send_typed(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.ask(text, str(QSettings().value('Assistant/effort', 'medium', type=str)), text)

    def ask(self, prompt:str, effort:str, shown:str) -> None:
        if self.chat.busy:
            return
        key, _ = load_key()
        if not key:
            self.add('Note', 'Set an API key first: tap “API key”.')
            self.edit_key()
            return
        self.add('You', shown)
        for b in (*self.quick_buttons, self.send_button):
            b.setEnabled(False)
        self.set_working('thinking…')
        self.chat.send(prompt, effort, key)

    def on_done(self) -> None:
        for b in (*self.quick_buttons, self.send_button):
            b.setEnabled(True)
        self.update_status()

    def add(self, who:str, text:str) -> None:
        self.transcript.append((who, text))
        self.show_transcript()

    def show_transcript(self) -> None:
        if not self.transcript:
            self.view.setMarkdown('Ask about the roast in progress, a past roast, or a tasting. Claude reads the roast '
                'data but cannot change the roaster.')
            return
        parts = []
        for who, text in self.transcript:
            parts.append(f'**{who}:** {text}' if who != 'Claude' else text)
        self.view.setMarkdown('\n\n---\n\n'.join(parts))
        bar = self.view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())


_panel:ClaudePanel|None = None


def install(aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
    if aw.app.artisanviewerMode:
        return
    from artisanlib.touch_controls import px_per_mm, add_menu_action
    mm = px_per_mm(aw)

    def show() -> None:
        global _panel # pylint: disable=global-statement
        if _panel is None:
            _panel = ClaudePanel(aw, assist, mm)
        _panel.show()
        _panel.raise_()
        _panel.activateWindow()

    action = QAction(QApplication.translate('Menu', 'Claude panel'), aw)
    action.triggered.connect(show)
    add_menu_action(aw, action)
    if assist is not None:
        b = QPushButton('Ask Claude')
        b.setObjectName('assist_claude')
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b.clicked.connect(show)
        assist.actions.addWidget(b, 2, 1)
