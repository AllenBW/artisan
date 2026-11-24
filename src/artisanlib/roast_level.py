#
# ABOUT
# artisan scope roast level indicator
#
# COPYRIGHT (C) 2010-2026 The artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
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
#
# MAINTAINER
# Marko Luther, 2026

# Roast level indicator: names the roast degree (Half City, City, City+, Full City) the beans
# reached so far, from configurable bean temperature limits.
#
# A bean probe does not read the true bean temperature. A probe in the bean mass sees some
# of the hot air too, and any probe lags the beans by a degree or more. The estimate therefore
# blends BT with the air temperatures by configurable weights and adds an offset, which lets
# the limits be taken from a roast chart while the probe is corrected for once:
#
#     estimate = (1 - w_ET - w_X) * BT + w_ET * ET + w_X * X + offset
#
# where X is an optional extra channel, like the exhaust temperature of a drum roaster.
# ET and X measure air, not beans, and read well above the beans late in a roast, which is
# why their weights are best kept small. The defaults (all weights 0, offset 0) use BT as read.
#
# A roast does not get lighter again, so the level is that of the highest estimate since the
# lowest BT after CHARGE, rather than the current one. This also keeps the indicator from
# flickering near a limit and ignores the high BT readings a preheated probe leaves before the
# beans cool it down. The lowest BT is used rather than Artisan's TP, as a TP placed too early
# (after a CHARGE detected during preheat) would hold on to a level the beans never reached.
#
# The countdown to the next level extends a straight line fitted to the latest estimates up to
# the next limit. The fit, rather than the last reading and its rate of rise, is what makes this
# work with probes reporting whole degrees only, where the last reading is up to a degree behind
# and the rate between two readings is either 0 or huge. The line is fitted to the last
# ROAST_LEVEL_ETA_WINDOW seconds, a window widened up to ROAST_LEVEL_ETA_WINDOW_MAX until the
# estimates in it rose by ROAST_LEVEL_ETA_MIN_RISE degrees, so that a slow rise in whole degrees
# still gives a line. Measured on 154 roasts of a Coffee-Tech FZ94 (whole degrees, a reading every
# 2 s, 413 limits reached): 30 s ahead the countdown was off by 5 s in the median and by more than
# 15 s one time in ten, 60 s ahead by 11 s and 32 s; a window of 60 s was off by a third more. No
# line through the past knows of the heat or air changed a moment ago.

from collections.abc import Sequence
from typing import Final, TYPE_CHECKING

import numpy

if TYPE_CHECKING:
    import numpy.typing as npt # pylint: disable=unused-import

# level names in order, each starting at the corresponding entry of the limits below
ROAST_LEVELS: Final[tuple[str, ...]] = ('Half City', 'City', 'City+', 'Full City')

# start temperatures of the ROAST_LEVELS, followed by the end of the last level, as bean
# temperatures: City (426F), City+ (435F), Full City (446F) and its end at Full City+ (454F,
# the first snaps of second crack) are those of Sweet Maria's pictorial guide to the roast
# process; Half City is not on that chart and starts here at its "first crack under way" (415F).
# The same guide has first crack begin at 401F
ROAST_LEVEL_LIMITS_F_CHART: Final[tuple[int, ...]] = (415, 426, 435, 446, 454)
ROAST_LEVEL_LIMITS_C_CHART: Final[tuple[int, ...]] = (213, 219, 224, 230, 234)

# the limits as this fork's roaster reads them, a Coffee-Tech FZ94 whose bean probe is well behind
# the beans: over its logged roasts first crack was marked at a median BT of 188.5C (126 roasts)
# and second crack at 212C (29 roasts), where the chart has 205C and 234.4C. The chart limits are
# placed between those two marks in proportion, as one offset cannot fit both: the 17C that fit
# first crack would have second crack begin while City+ is still shown.
# These are the defaults, in the temperature mode the limits are kept in
ROAST_LEVEL_LIMITS_C_DEFAULT: Final[tuple[int, ...]] = (195, 200, 204, 209, 212)
ROAST_LEVEL_LIMITS_F_DEFAULT: Final[tuple[int, ...]] = (383, 392, 399, 408, 414)

# the countdown to the next level is taken from the estimates of the last this many seconds,
ROAST_LEVEL_ETA_WINDOW: Final[float] = 30.
# widened in steps of 10 s up to this many, until the estimates in the window rose by at least
ROAST_LEVEL_ETA_WINDOW_MAX: Final[float] = 90.
# this many degrees
ROAST_LEVEL_ETA_MIN_RISE: Final[float] = 3.
# no countdown below this rate of rise of the estimate, in degrees per minute
ROAST_LEVEL_ETA_MIN_RATE: Final[float] = 0.5
# from this many seconds on the countdown is highlighted, and with a target level set, an alert given
ROAST_LEVEL_ETA_WARN: Final[float] = 30.
# a countdown of more than this many seconds is not shown: too far off to be told, or to matter
ROAST_LEVEL_ETA_SHOWN: Final[float] = 300.

# background colors of the ROAST_LEVELS, followed by the one for beyond the last level
ROAST_LEVEL_COLORS: Final[tuple[str, ...]] = ('#b5835a', '#9a6a43', '#83552f', '#6b4123', '#4a2a14')


def estimate_bean_temp(bt:float, et:float, et_weight:float, offset:float, xt:float = 0., xt_weight:float = 0.) -> float:
    """Returns the bean temperature estimated from the BT, ET and extra channel X readings.
    et_weight and xt_weight are the shares of ET and X in the estimate, BT taking the rest."""
    return (1 - et_weight - xt_weight) * bt + et_weight * et + xt_weight * xt + offset


def _estimates(bt:Sequence[float], et:Sequence[float], start:int, end:int,
        et_weight:float, offset:float, xt:Sequence[float]|None, xt_weight:float) -> 'tuple[npt.NDArray[numpy.double], npt.NDArray[numpy.bool_]]':
    """Returns the bean temperature estimates of the readings from index start up to, but excluding,
    end, and which of them are valid. Readings of -1 are missing ones, and a reading missing any of
    the channels weighted in is not valid."""
    if xt is None: # no extra channel selected
        xt_weight = 0
    channels:list[tuple[Sequence[float], float]] = [(bt, 1 - et_weight - xt_weight)]
    if et_weight > 0:
        channels.append((et, et_weight))
    if xt is not None and xt_weight > 0:
        channels.append((xt, xt_weight))
    n = max(0, min(end, *(len(c) for c, _ in channels)) - start) if start >= 0 else 0
    estimates = numpy.full(n, offset, dtype=numpy.double)
    valid = numpy.ones(n, dtype=bool)
    for c, w in channels:
        values = numpy.array(c[start:start+n], dtype=numpy.double)
        valid &= (values != -1) & ~numpy.isnan(values)
        estimates += w * values
    return estimates, valid


def max_estimated_temp(bt:Sequence[float], et:Sequence[float], start:int, end:int,
        et_weight:float, offset:float, xt:Sequence[float]|None = None, xt_weight:float = 0.) -> float|None:
    """Returns the highest bean temperature estimate over the readings from index start up to,
    but excluding, end, or None if there is no valid reading."""
    estimates, valid = _estimates(bt, et, start, end, et_weight, offset, xt, xt_weight)
    if not valid.any():
        return None
    return float(numpy.max(estimates[valid]))


def seconds_to_temp(target:float, timex:Sequence[float], bt:Sequence[float], et:Sequence[float], start:int, end:int,
        et_weight:float, offset:float, xt:Sequence[float]|None = None, xt_weight:float = 0.,
        window:float = ROAST_LEVEL_ETA_WINDOW, window_max:float = ROAST_LEVEL_ETA_WINDOW_MAX) -> float|None:
    """Returns the seconds until the bean temperature estimate reaches target, 0 if it is there by now,
    or None if this cannot be told: too few readings before the last one (from index start up to, but
    excluding, end), or an estimate that rises too slowly or falls. The readings of the last window
    seconds count, of up to window_max seconds while they rose by less than ROAST_LEVEL_ETA_MIN_RISE."""
    end = min(end, len(timex))
    estimates, valid = _estimates(bt, et, start, end, et_weight, offset, xt, xt_weight)
    if not valid.any():
        return None
    times = numpy.array(timex[start:start+len(estimates)], dtype=numpy.double)
    now = times[-1]
    while True:
        recent = valid & (times >= now - window)
        if window >= window_max or (recent.any() and numpy.ptp(estimates[recent]) >= ROAST_LEVEL_ETA_MIN_RISE):
            break
        window = min(window + 10, window_max)
    # a line through readings covering less than a third of the window says little about the trend
    if numpy.count_nonzero(recent) < 4 or times[recent][-1] - times[recent][0] < window / 3:
        return None
    slope, current = numpy.polyfit(times[recent] - now, estimates[recent], 1)
    if slope * 60 < ROAST_LEVEL_ETA_MIN_RATE:
        return None
    return max(0., float((target - current) / slope))


def rise_start(bt:Sequence[float], charge:int, end:int) -> int|None:
    """Returns the index of the lowest valid BT reading from index charge up to, but excluding, end,
    the last of them if there are several, or None if there is none: from there on the beans rise."""
    end = min(end, len(bt))
    if charge < 0 or charge >= end:
        return None
    values = numpy.array(bt[charge:end], dtype=numpy.double)
    values[(values == -1) | numpy.isnan(values)] = numpy.inf
    if not numpy.isfinite(values).any():
        return None
    return charge + len(values) - 1 - int(numpy.argmin(values[::-1]))


def next_limit(temp:float|None, limits:Sequence[float]) -> int|None:
    """Returns the index of the first of the limits above temp, the one a rising roast reaches next,
    or None if temp is unknown or beyond them all."""
    if temp is None:
        return None
    for i, limit in enumerate(limits):
        if temp < limit:
            return i
    return None


def roast_level(temp:float|None, limits:Sequence[float]) -> int|None:
    """Returns the index into ROAST_LEVELS of the level temp falls into, len(ROAST_LEVELS)
    if temp is beyond the end of the last level, or None if temp is below the first level."""
    if temp is None or len(limits) <= len(ROAST_LEVELS) or temp < limits[0]:
        return None
    for i in range(len(ROAST_LEVELS)):
        if temp < limits[i + 1]:
            return i
    return len(ROAST_LEVELS)
