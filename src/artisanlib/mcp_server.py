#
# ABOUT
# Optional MCP server that lets Claude (Claude Code or the Claude desktop app, with the user's own Claude login)
# follow the live roast and look up past roasts and tastings, to guide and teach while roasting.
#
# It is read-only towards the machine: no tool changes fan, drum, heat, marks or anything sent over MODBUS.
# The only writes are a note on the current roast and a tasting entry (artisanlib/roast_tools.py).
#
# Runs on its own thread with streamable HTTP, bound to 127.0.0.1 only (default port 8765, path /mcp).
# Enabled with the environment variable ARTISAN_MCP=1 or View > Claude Assistant (MCP server).
# Requires the 'mcp' package (2.x); without it the menu entry says so and nothing else changes.

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
import asyncio
import logging
import threading
from typing import Final, Any, TYPE_CHECKING

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication

from artisanlib.roast_tools import RoastTools, INSTRUCTIONS, DESCRIPTIONS

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import
    from artisanlib.roast_assist import RoastAssist # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_PORT: Final[int] = 8765


class RoastMcpServer:
    def __init__(self, aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
        self.aw = aw
        self.tools = RoastTools(aw, assist)
        self.port = int(QSettings().value('Assistant/mcpPort', DEFAULT_PORT, type=int))
        self.thread:threading.Thread|None = None
        self.error:str = ''

    def build(self) -> Any:
        from mcp.server.mcpserver import MCPServer # type: ignore[import-not-found,unused-ignore] # pylint: disable=import-error
        server = MCPServer('artisan-roaster', instructions=INSTRUCTIONS)
        tools = self.tools
        d = DESCRIPTIONS

        @server.tool(description=d['get_live_roast']) # type: ignore[untyped-decorator,unused-ignore]
        def get_live_roast() -> dict[str, Any]:
            return tools.live()

        @server.tool(description=d['get_roast_curve']) # type: ignore[untyped-decorator,unused-ignore]
        def get_roast_curve(step_s:float = 10.0) -> dict[str, Any]:
            return tools.curve(step_s)

        @server.tool(description=d['list_roasts']) # type: ignore[untyped-decorator,unused-ignore]
        def list_roasts(bean:str = '', min_weight_g:float = 0.0, max_weight_g:float = 0.0, limit:int = 20) -> list[dict[str, Any]]:
            return tools.list_roasts(bean, min_weight_g, max_weight_g, limit)

        @server.tool(description=d['get_roast']) # type: ignore[untyped-decorator,unused-ignore]
        def get_roast(name:str, step_s:float = 10.0) -> dict[str, Any]:
            return tools.get_roast(name, step_s)

        @server.tool(description=d['compare_with_history']) # type: ignore[untyped-decorator,unused-ignore]
        def compare_with_history() -> dict[str, Any]:
            return tools.compare_with_history()

        @server.tool(description=d['list_tastings']) # type: ignore[untyped-decorator,unused-ignore]
        def list_tastings(roast:str = '', bean:str = '', method:str = '', limit:int = 30) -> list[dict[str, Any]]:
            return tools.list_tastings(roast, bean, method, limit)

        @server.tool(description=d['add_tasting']) # type: ignore[untyped-decorator,unused-ignore]
        def add_tasting(roast:str, method:str, score:int = 0, development:str = '', grinder:str = '', grind:str = '',
                dose_g:float = 0.0, yield_g:float = 0.0, time_s:float = 0.0, descriptors:list[str]|None = None, notes:str = '') -> dict[str, Any]:
            return tools.add_tasting(roast, method, score, development, grinder, grind, dose_g, yield_g, time_s, descriptors, notes)

        @server.tool(description=d['add_roast_note']) # type: ignore[untyped-decorator,unused-ignore]
        def add_roast_note(text:str) -> str:
            return tools.add_roast_note(text)

        return server

    def start(self) -> bool:
        if self.thread is not None and self.thread.is_alive():
            return True
        try:
            server = self.build()
        except ImportError:
            self.error = "the 'mcp' package is not installed (run dev\\setup-dev.ps1)"
            return False
        except Exception as e: # pylint: disable=broad-except
            self.error = str(e)
            _log.exception(e)
            return False
        def run() -> None:
            try:
                asyncio.run(server.run_streamable_http_async(host='127.0.0.1', port=self.port))
            except Exception as e: # pylint: disable=broad-except
                self.error = str(e)
                _log.exception(e)
        self.thread = threading.Thread(target=run, daemon=True, name='artisan-mcp')
        self.thread.start()
        _log.info('MCP server: http://127.0.0.1:%d/mcp', self.port)
        return True


_instance:RoastMcpServer|None = None


def install(aw:'ApplicationWindow', assist:'RoastAssist|None') -> None:
    global _instance # pylint: disable=global-statement
    if _instance is not None or aw.app.artisanviewerMode:
        return
    _instance = RoastMcpServer(aw, assist)
    server = _instance
    enabled = os.environ.get('ARTISAN_MCP', '').strip() == '1' or bool(QSettings().value('Assistant/mcpEnabled', False, type=bool))
    action = QAction(QApplication.translate('Menu', 'Claude Assistant (MCP server)'), aw)
    action.setCheckable(True)
    def toggled(on:bool) -> None:
        QSettings().setValue('Assistant/mcpEnabled', on)
        if on:
            if server.start():
                aw.sendmessage(f'Claude assistant: MCP server on http://127.0.0.1:{server.port}/mcp')
            else:
                action.setChecked(False)
                aw.sendmessage(f'Claude assistant not started: {server.error}')
        else:
            aw.sendmessage('Claude assistant: the MCP server stops when Artisan is restarted')
    action.toggled.connect(toggled)
    from artisanlib.touch_controls import add_menu_action
    add_menu_action(aw, action)
    if enabled:
        action.setChecked(True) # starts the server
