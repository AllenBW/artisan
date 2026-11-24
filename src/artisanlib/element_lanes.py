#
# ABOUT
# The heating elements (left, center, right) of the FZ94: switched on and off by hand, with no intensity setting.
# Each change is logged as an event of type 'Elements' (event type 2, Artisan's unused 'Damper'): its value is
# 10 x the number of elements on, its description names the ones on ("Left off (elements C+R on)",
# "at start: elements L+C+R on", "all elements off"). From those events this module works out when each element
# was on, and draws one lane per element on the graph: a faint track across the graph with a bar while the element is
# on, so that a gap in the bars reads as off. Bars still open grow with the roast, drawn on each live update like
# Artisan's time guide.
#
# While recording the lanes run along the top of the graph, which is free then. Afterwards Artisan writes the phase
# statistics there (times and percentages, the phases bar, a line of temperatures): the lanes then sit right below
# them, time-aligned with the phases bar like further rows of it. The bars are drawn below the curves, never over them.
#
# The events themselves are not shown on the graph the way Artisan shows events (a step line of the number of elements
# on, with its labels): the lanes say the same and more.

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
TRACK_COLOR: Final[str] = '#808080'
LANE_TOP: Final[float] = 0.975 # top of the first lane while recording, in axes fractions
LANE_HEIGHT: Final[float] = 0.018
LANE_GAP: Final[float] = 0.007
LANE_ZORDER: Final[float] = 1.5 # above the phase watermarks, below the curves and their labels

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
        for on, off in ps:
            if off is not None and off <= charge:
                continue # over before the beans went in
            start = max(on, charge) # on since the recording started: counted from CHARGE
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
        self.bars:list[Rectangle] = [] # one per on period
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

    # top of the first lane, in axes fractions
    def lane_top(self) -> float:
        qmc = self.qmc
        if qmc.flagstart:
            return LANE_TOP
        # below the phase statistics, placed as in tgraphcanvas.writestatistics(): the phases bar 8% below the top of the
        # graph (13% with the legend on top), the line of text under it 3.5 bar heights lower (2.5 in Fahrenheit), a bar
        # height being 1/70 of the graph
        text = (0.13 if qmc.legendloc in {1, 2, 9} else 0.08) + (3.5 if qmc.mode == 'C' else 2.5) / 70
        return 1 - text - 0.012

    def draw(self) -> None:
        self.artists, self.bars, self.open_bars = [], [], []
        ax = self.qmc.ax
        if ax is None or not self.qmc.timex:
            return
        periods = self.periods()
        if not any(periods):
            return
        trans = blended_transform_factory(ax.transData, ax.transAxes)
        recording = self.qmc.flagstart
        last = self.qmc.timex[-1]
        top = self.lane_top()
        for k, (short, _) in enumerate(ELEMENTS):
            y = top - LANE_HEIGHT - k * (LANE_HEIGHT + LANE_GAP)
            label = ax.text(0.003, y + LANE_HEIGHT / 2, short, transform=ax.transAxes, ha='left', va='center',
                fontsize='x-small', fontweight='bold', color=ON_COLOR, zorder=30)
            self.artists.append(label)
            # the track: without it an element that is off would look the same as no lane at all
            track = Rectangle((0, y), 1, LANE_HEIGHT, transform=ax.transAxes, facecolor=TRACK_COLOR, edgecolor='none', alpha=0.13,
                zorder=LANE_ZORDER - 0.1)
            ax.add_patch(track)
            self.artists.append(track)
            for start, end in periods[k]:
                bar = Rectangle((start, y), (end if end is not None else last) - start, LANE_HEIGHT, transform=trans,
                    facecolor=ON_COLOR, edgecolor='none', alpha=0.75, zorder=LANE_ZORDER)
                if end is None and recording:
                    bar.set_animated(True) # grows on each live update, not cached in the background
                    self.open_bars.append((bar, start))
                ax.add_patch(bar)
                self.artists.append(bar)
                self.bars.append(bar)

    def draw_live(self) -> None:
        ax = self.qmc.ax
        if ax is None or not self.open_bars or not self.qmc.timex:
            return
        now = self.qmc.timex[-1]
        for bar, start in self.open_bars:
            bar.set_width(max(0.0, now - start))
            ax.draw_artist(bar)
