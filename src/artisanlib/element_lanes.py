#
# ABOUT
# The heating elements (left, center, right) of the FZ94: switched on and off by hand, with no intensity setting.
# Each change is logged as an event of type 'Elements' (event type 2, Artisan's unused 'Damper'): its value is
# 10 x the number of elements on, its description names the ones on ("Left off (elements C+R on)",
# "at charge: elements L+C+R on", "all elements off"). From those events this module works out when each element
# was on, and draws one lane per element along the top of the graph: a bar while the element is on. Bars still
# open grow with the roast, drawn on each live update like Artisan's time guide.

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
import logging
from typing import Final, Any, TYPE_CHECKING
from collections.abc import Sequence

from matplotlib.artist import Artist
from matplotlib.patches import Rectangle
from matplotlib.transforms import blended_transform_factory

if TYPE_CHECKING:
    from artisanlib.canvas import tgraphcanvas # pylint: disable=unused-import

_log: Final[logging.Logger] = logging.getLogger(__name__)

ELEMENTS: Final[list[tuple[str, str]]] = [('L', 'Left'), ('C', 'Center'), ('R', 'Right')]
ELEMENTS_EVENT_TYPE: Final[int] = 2 # the event type logged for the heating elements (Artisan's unused 'Damper')
ON_COLOR: Final[str] = '#e05a3c'
LANE_TOP: Final[float] = 0.975 # top of the first lane, in axes fractions
LANE_HEIGHT: Final[float] = 0.018
LANE_GAP: Final[float] = 0.007

STATE: Final = re.compile(r'elements ([LCR](?:\+[LCR])*) on')

Period = tuple[float, float|None] # start, end (None: still on), in the time base of timex


def state_text(state:Sequence[bool]) -> str:
    on = [short for (short, _), s in zip(ELEMENTS, state, strict=True) if s]
    return ('elements ' + '+'.join(on) + ' on') if on else 'all elements off'


# the elements on after an Elements event, from its description; None if it names no state
def parse_state(description:str) -> tuple[bool, ...]|None:
    if 'all elements off' in description:
        return (False,) * len(ELEMENTS)
    m = STATE.search(description)
    if m is None:
        return None
    on = set(m.group(1).split('+'))
    return tuple(short in on for short, _ in ELEMENTS)


def element_periods(timex:Sequence[float], events:Sequence[int], types:Sequence[int], descriptions:Sequence[str]) -> list[list[Period]]:
    """Per element (left, center, right) the periods it was on, from the Elements events of a roast."""
    periods:list[list[Period]] = [[] for _ in ELEMENTS]
    on_since:list[float|None] = [None] * len(ELEMENTS)
    changes = sorted((idx, descriptions[n] if n < len(descriptions) else '') for n, (idx, etype) in enumerate(zip(events, types, strict=False))
        if etype == ELEMENTS_EVENT_TYPE and 0 <= idx < len(timex))
    for idx, description in changes:
        state = parse_state(description)
        if state is None:
            continue
        t = timex[idx]
        for k, on in enumerate(state):
            since = on_since[k]
            if on and since is None:
                on_since[k] = t
            elif not on and since is not None:
                periods[k].append((since, t))
                on_since[k] = None
    for k, since in enumerate(on_since):
        if since is not None:
            periods[k].append((since, None))
    return periods


def summary(periods:list[list[Period]], charge:float, end:float|None) -> dict[str, Any]:
    """On periods (seconds after CHARGE; 'off' None: still on) and the total on time per element, counting an element
    still on up to end (eg. now, or DROP)."""
    result:dict[str, Any] = {}
    for (_, name), ps in zip(ELEMENTS, periods, strict=True):
        rows = []
        total = 0.0
        for start, off in ps:
            rows.append({'on': round(start - charge, 1), 'off': round(off - charge, 1) if off is not None else None})
            stop = off if off is not None else end
            if stop is not None:
                total += max(0.0, stop - start)
        result[name.lower()] = {'on_periods': rows, 'on_time_s': round(total, 1)}
    return result


class ElementLanes:
    """Draws the element lanes on the graph through the canvas redraw and live hooks."""

    def __init__(self, qmc:'tgraphcanvas') -> None:
        self.qmc = qmc
        self.artists:list[Artist] = []
        self.open_bars:list[tuple[Rectangle, float]] = [] # (bar, start) of the elements still on while recording
        qmc.redraw_hooks.append(self.draw)
        qmc.live_hooks.append(self.draw_live)

    def periods(self) -> list[list[Period]]:
        qmc = self.qmc
        return element_periods(qmc.timex, qmc.specialevents, qmc.specialeventstype, qmc.specialeventsStrings)

    # after an Elements event: the lanes again, without a full redraw
    def update(self) -> None:
        for a in self.artists:
            try:
                a.remove()
            except Exception: # pylint: disable=broad-except
                pass # gone with the last redraw
        self.draw()
        self.qmc.updateBackground()

    def draw(self) -> None:
        self.artists, self.open_bars = [], []
        ax = self.qmc.ax
        if ax is None or not self.qmc.timex:
            return
        periods = self.periods()
        if not any(periods):
            return
        trans = blended_transform_factory(ax.transData, ax.transAxes)
        recording = self.qmc.flagstart
        last = self.qmc.timex[-1]
        for k, (short, _) in enumerate(ELEMENTS):
            y = LANE_TOP - LANE_HEIGHT - k * (LANE_HEIGHT + LANE_GAP)
            label = ax.text(0.003, y + LANE_HEIGHT / 2, short, transform=ax.transAxes, ha='left', va='center',
                fontsize='x-small', fontweight='bold', color=ON_COLOR, zorder=30)
            self.artists.append(label)
            for start, end in periods[k]:
                bar = Rectangle((start, y), (end if end is not None else last) - start, LANE_HEIGHT, transform=trans,
                    facecolor=ON_COLOR, edgecolor='none', alpha=0.75, zorder=30)
                if end is None and recording:
                    bar.set_animated(True) # grows on each live update, not cached in the background
                    self.open_bars.append((bar, start))
                ax.add_patch(bar)
                self.artists.append(bar)

    def draw_live(self) -> None:
        ax = self.qmc.ax
        if ax is None or not self.open_bars or not self.qmc.timex:
            return
        now = self.qmc.timex[-1]
        for bar, start in self.open_bars:
            bar.set_width(max(0.0, now - start))
            ax.draw_artist(bar)
