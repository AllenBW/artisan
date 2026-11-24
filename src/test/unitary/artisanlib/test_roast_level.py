"""Unit tests for artisanlib.roast_level module.

Tests the bean temperature estimate from BT, ET and an extra channel, the highest estimate
since TP, the mapping of a temperature onto the configured roast level limits and the
countdown to the next limit.
"""

import math

import pytest

from artisanlib.roast_level import (ROAST_LEVELS, ROAST_LEVEL_LIMITS_F_DEFAULT, ROAST_LEVEL_LIMITS_C_DEFAULT,
    ROAST_LEVEL_LIMITS_F_CHART, ROAST_LEVEL_LIMITS_C_CHART, ROAST_LEVEL_COLORS, ROAST_LEVEL_ETA_WINDOW, ROAST_LEVEL_ETA_WINDOW_MAX,
    estimate_bean_temp, max_estimated_temp, next_limit, rise_start, roast_level, seconds_to_temp)


class TestDefaults:
    def test_limits_match_levels(self) -> None:
        for limits in (ROAST_LEVEL_LIMITS_F_DEFAULT, ROAST_LEVEL_LIMITS_C_DEFAULT, ROAST_LEVEL_LIMITS_F_CHART, ROAST_LEVEL_LIMITS_C_CHART):
            assert len(limits) == len(ROAST_LEVELS) + 1
            assert list(limits) == sorted(set(limits))
        assert len(ROAST_LEVEL_COLORS) == len(ROAST_LEVELS) + 1

    # switching the temperature mode converts and rounds the limits: the defaults of both modes are the same ones
    def test_celsius_and_fahrenheit_agree(self) -> None:
        for fahrenheit, celsius in ((ROAST_LEVEL_LIMITS_F_DEFAULT, ROAST_LEVEL_LIMITS_C_DEFAULT), (ROAST_LEVEL_LIMITS_F_CHART, ROAST_LEVEL_LIMITS_C_CHART)):
            for f, c in zip(fahrenheit, celsius, strict=True):
                assert round((f - 32) * 5 / 9) == c
                assert round(c * 9 / 5 + 32) == f or fahrenheit is ROAST_LEVEL_LIMITS_F_CHART

    # the defaults are the chart limits placed between the first and second crack this fork's roaster reads
    # (188.5C and 212C) in proportion to where the chart has them (205C and 234.4C)
    def test_defaults_follow_the_chart_between_the_cracks(self) -> None:
        for chart, default in zip(ROAST_LEVEL_LIMITS_C_CHART, ROAST_LEVEL_LIMITS_C_DEFAULT, strict=True):
            assert default == pytest.approx(188.5 + (chart - 205) / (234.4 - 205) * (212 - 188.5), abs=0.75)


class TestEstimateBeanTemp:
    def test_bt_only(self) -> None:
        assert estimate_bean_temp(430, 480, 0, 0) == 430

    def test_offset(self) -> None:
        assert estimate_bean_temp(430, 480, 0, 5) == 435

    def test_blend(self) -> None:
        assert estimate_bean_temp(430, 480, 0.2, 0) == pytest.approx(440)

    def test_et_only(self) -> None:
        assert estimate_bean_temp(430, 480, 1, -2) == 478

    def test_blend_with_exhaust(self) -> None:
        # 70% BT, 10% ET, 20% exhaust
        assert estimate_bean_temp(220, 260, 0.1, 0, 230, 0.2) == pytest.approx(154 + 26 + 46)


class TestMaxEstimatedTemp:
    def test_max_since_start(self) -> None:
        bt = [500., 300., 400., 438., 436.]
        et = [500., 450., 460., 470., 470.]
        # the preheated reading before start is ignored
        assert max_estimated_temp(bt, et, 1, len(bt), 0, 0) == 438

    def test_end_excluded(self) -> None:
        bt = [300., 400., 438., 450.]
        et = [450., 460., 470., 480.]
        assert max_estimated_temp(bt, et, 0, 3, 0, 0) == 438

    def test_missing_readings_skipped(self) -> None:
        bt = [300., -1., math.nan, 420.]
        et = [450., 460., 470., -1.]
        assert max_estimated_temp(bt, et, 0, len(bt), 0, 0) == 420
        # with ET in the estimate, a reading missing either value is skipped
        assert max_estimated_temp(bt, et, 0, len(bt), 0.5, 0) == 375

    def test_exhaust(self) -> None:
        bt = [180., 210., 222.]
        et = [240., 250., 255.]
        xt = [200., 220., -1.]
        # the last reading misses the exhaust and is skipped
        assert max_estimated_temp(bt, et, 0, len(bt), 0, 0, xt, 0.5) == pytest.approx(215)
        # without a weight, the exhaust is not read
        assert max_estimated_temp(bt, et, 0, len(bt), 0, 0, xt, 0) == 222
        assert max_estimated_temp(bt, et, 0, len(bt), 0, 1, None, 0.5) == 223

    def test_no_valid_reading(self) -> None:
        assert max_estimated_temp([-1.], [-1.], 0, 1, 0, 0) is None
        assert max_estimated_temp([], [], 0, 0, 0, 0) is None
        assert max_estimated_temp([400.], [400.], 5, 10, 0, 0) is None
        assert max_estimated_temp([400.], [400.], -1, 1, 0, 0) is None


class TestRoastLevel:
    limits = ROAST_LEVEL_LIMITS_F_CHART # 415, 426, 435, 446, 454

    def test_below_first_level(self) -> None:
        assert roast_level(None, self.limits) is None
        assert roast_level(414.9, self.limits) is None

    @pytest.mark.parametrize(('temp', 'expected'), [
        (415, 'Half City'),
        (425.9, 'Half City'),
        (426, 'City'),
        (435, 'City+'),
        (445.9, 'City+'),
        (446, 'Full City'),
        (453.9, 'Full City'),
    ])
    def test_levels(self, temp:float, expected:str) -> None:
        level = roast_level(temp, self.limits)
        assert level is not None
        assert ROAST_LEVELS[level] == expected

    @pytest.mark.parametrize(('temp', 'expected'), [
        (212.9, None),
        (213, 'Half City'),
        (219, 'City'),
        (224, 'City+'),
        (230, 'Full City'),
        (234, 'beyond'),
    ])
    def test_celsius_levels(self, temp:float, expected:str|None) -> None:
        level = roast_level(temp, ROAST_LEVEL_LIMITS_C_CHART)
        if expected is None:
            assert level is None
        elif expected == 'beyond':
            assert level == len(ROAST_LEVELS)
        else:
            assert level is not None
            assert ROAST_LEVELS[level] == expected

    def test_beyond_last_level(self) -> None:
        assert roast_level(454, self.limits) == len(ROAST_LEVELS)

    def test_incomplete_limits(self) -> None:
        assert roast_level(430, [415, 426]) is None


class TestRiseStart:
    def test_lowest_reading_after_charge(self) -> None:
        #     preheat    charge              lowest       rising
        bt = [200., 201., 224., 180., 120., 86., 86., 90., 120., 190.]
        assert rise_start(bt, 2, len(bt)) == 6 # the last of the lowest readings

    def test_charge_marked_during_preheat(self) -> None:
        # CHARGE detected long before the beans went in: the high readings up to the real charge do not count
        bt = [200., 202., 205., 207., 150., 90., 84., 95., 150.]
        start = rise_start(bt, 0, len(bt))
        assert start == 6
        assert max_estimated_temp(bt, bt, start, len(bt), 0, 0) == 150

    def test_still_falling(self) -> None:
        assert rise_start([224., 180., 120.], 0, 3) == 2

    def test_missing_readings_skipped(self) -> None:
        assert rise_start([224., -1., 120., math.nan, 130.], 0, 5) == 2

    def test_end_excluded(self) -> None:
        assert rise_start([224., 180., 120., 80.], 0, 3) == 2

    def test_nothing_to_go_by(self) -> None:
        assert rise_start([224., 180.], -1, 2) is None
        assert rise_start([224., 180.], 5, 9) is None
        assert rise_start([-1., -1.], 0, 2) is None
        assert rise_start([], 0, 0) is None


class TestNextLimit:
    limits = ROAST_LEVEL_LIMITS_C_CHART # 213, 219, 224, 230, 234

    def test_next_limit(self) -> None:
        assert next_limit(None, self.limits) is None
        assert next_limit(190, self.limits) == 0 # Half City next
        assert next_limit(213, self.limits) == 1 # at Half City, City next
        assert next_limit(229.9, self.limits) == 3
        assert next_limit(230, self.limits) == 4 # the end of Full City
        assert next_limit(234, self.limits) is None


class TestSecondsToTemp:

    # a roast sampled every 2 s, rising by rate degrees per minute up to bt_now at the last sample
    @staticmethod
    def roast(bt_now:float, rate:float, samples:int = 60, whole_degrees:bool = False) -> tuple[list[float], list[float], list[float]]:
        timex = [2. * i for i in range(samples)]
        bt = [bt_now - rate / 60 * (timex[-1] - t) for t in timex]
        if whole_degrees:
            bt = [float(int(v)) for v in bt]
        return timex, bt, [v + 30 for v in bt]

    def test_steady_rise(self) -> None:
        timex, bt, et = self.roast(204, 6) # 6 degrees per minute, 3 degrees to go
        assert seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0) == pytest.approx(30)

    def test_offset(self) -> None:
        timex, bt, et = self.roast(204, 6)
        # the estimate reads 17 above BT: 221 now, 224 in 30 s
        assert seconds_to_temp(224, timex, bt, et, 0, len(bt), 0, 17) == pytest.approx(30)

    def test_blend(self) -> None:
        timex, bt, et = self.roast(204, 6)
        xt = [v + 10 for v in bt]
        # 10% ET (+30) and 20% of the extra channel (+10) read 5 above BT
        assert seconds_to_temp(212, timex, bt, et, 0, len(bt), 0.1, 0, xt, 0.2) == pytest.approx(30)

    def test_reached(self) -> None:
        timex, bt, et = self.roast(204, 6)
        assert seconds_to_temp(203, timex, bt, et, 0, len(bt), 0, 0) == 0

    def test_whole_degrees(self) -> None:
        # a probe reporting whole degrees at 4 degrees per minute steps only every 15 s:
        # the last reading says 203 while the beans are at 203.9, 46.5 s from 207
        timex, bt, et = self.roast(203.9, 4, whole_degrees=True)
        assert bt[-1] == 203
        secs = seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0)
        assert secs is not None
        assert secs == pytest.approx(46.5, abs=12)

    def test_only_recent_readings_count(self) -> None:
        # a fast start, then flat for longer than the window: nothing to count down
        timex = [2. * i for i in range(90)]
        bt = [150. + i for i in range(45)] + [195.] * 45
        assert ROAST_LEVEL_ETA_WINDOW_MAX <= 2 * 45
        assert seconds_to_temp(207, timex, bt, bt, 0, len(bt), 0, 0) is None

    def test_slow_rise_in_whole_degrees(self) -> None:
        # 2 degrees per minute steps only every 30 s: the first window holds one step at most, the widened one three
        timex, bt, et = self.roast(203.9, 2, samples=120, whole_degrees=True)
        secs = seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0)
        assert secs is not None
        assert secs == pytest.approx(93, abs=25)
        # which the window as it is, not widened, gets badly wrong
        narrow = seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0, window_max=ROAST_LEVEL_ETA_WINDOW)
        assert narrow is None or abs(narrow - 93) > 25

    def test_recent_change_of_rate_counts(self) -> None:
        # 3 degrees per minute for long, then 12 for the last 30 s: the countdown follows the last window, not the minutes before
        timex = [2. * i for i in range(120)]
        bt = [150. + 3 / 60 * t for t in timex[:105]]
        bt += [bt[-1] + 12 / 60 * (t - timex[104]) for t in timex[105:]]
        assert seconds_to_temp(bt[-1] + 4, timex, bt, bt, 0, len(bt), 0, 0) == pytest.approx(20, abs=3)

    def test_falling_or_slow(self) -> None:
        timex, bt, et = self.roast(204, -3)
        assert seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0) is None
        timex, bt, et = self.roast(204, 0.3)
        assert seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0) is None

    def test_too_few_readings(self) -> None:
        timex, bt, et = self.roast(204, 6, samples=3)
        assert seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0) is None
        # readings covering only a small part of the window
        timex, bt, et = self.roast(204, 6, samples=8)
        assert seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0) is None
        assert seconds_to_temp(207, [], [], [], 0, 0, 0, 0) is None

    def test_missing_readings_skipped(self) -> None:
        timex, bt, et = self.roast(204, 6)
        for i in (40, 41, 50):
            bt[i] = -1
        assert seconds_to_temp(207, timex, bt, et, 0, len(bt), 0, 0) == pytest.approx(30)

    def test_start_and_end(self) -> None:
        timex, bt, et = self.roast(204, 6)
        # up to the 50th reading only, 20 s and 2 degrees before the last
        assert seconds_to_temp(207, timex, bt, et, 10, 50, 0, 0) == pytest.approx(50)
        assert seconds_to_temp(207, timex, bt, et, 70, len(bt), 0, 0) is None
        assert seconds_to_temp(207, timex, bt, et, -1, len(bt), 0, 0) is None
