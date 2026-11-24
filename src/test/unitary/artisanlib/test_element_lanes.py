"""Unit tests for artisanlib.element_lanes module.

Tests how the on periods of the heating elements are worked out from the Elements events of a roast,
and their summary relative to CHARGE.
"""

from artisanlib.element_lanes import ELEMENTS_EVENT_TYPE, Period, element_periods, parse_state, state_text, summary


class TestState:
    def test_round_trip(self) -> None:
        for state in ((True, True, True), (True, False, True), (False, False, True), (False, False, False)):
            assert parse_state(f'Right off ({state_text(state)})') == state

    def test_texts(self) -> None:
        assert state_text([True, True, True]) == 'elements L+C+R on'
        assert state_text([False, False, False]) == 'all elements off'
        assert parse_state('at start: elements L+C+R on') == (True, True, True)
        assert parse_state('a note typed by hand') is None


class TestElementPeriods:
    timex = [2. * i for i in range(400)] # a reading every 2 s

    def periods(self, events:list[tuple[int, str]], other:list[tuple[int, int, str]]|None = None) -> list[list[Period]]:
        rows = [(i, ELEMENTS_EVENT_TYPE, s) for i, s in events] + (other or [])
        return element_periods(self.timex, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])

    def test_all_on_from_the_start(self) -> None:
        assert self.periods([(0, 'at start: elements L+C+R on')]) == [[(0., None)], [(0., None)], [(0., None)]]

    def test_off_and_on_again(self) -> None:
        periods = self.periods([(0, 'at start: elements L+C+R on'), (180, 'Right off (elements L+C on)'), (240, 'Right on (elements L+C+R on)')])
        assert periods == [[(0., None)], [(0., None)], [(0., 360.), (480., None)]]

    def test_other_events_ignored(self) -> None:
        periods = self.periods([(0, 'at start: elements L+C+R on')], other=[(50, 0, '120Hz'), (60, ELEMENTS_EVENT_TYPE, 'no state named')])
        assert periods == [[(0., None)], [(0., None)], [(0., None)]]

    def test_no_events(self) -> None:
        assert self.periods([]) == [[], [], []]

    # roasts recorded before the state was logged at the start of the recording
    def test_state_logged_at_charge(self) -> None:
        assert self.periods([(7, 'at charge: elements L+C+R on')])[0] == [(14., None)]


class TestSummary:
    def test_counted_from_charge(self) -> None:
        # all on from the start of the recording, CHARGE 20 s in, right off from 6:00 to 8:00 after it, DROP at 11:00
        periods:list[list[Period]] = [[(0., None)], [(0., None)], [(0., 380.), (500., None)]]
        result = summary(periods, 20., 680.)
        assert result['left'] == {'on_periods': [{'on': 0., 'off': None}], 'on_time_s': 660.}
        assert result['right'] == {'on_periods': [{'on': 0., 'off': 360.}, {'on': 480., 'off': None}], 'on_time_s': 540.}

    def test_switched_off_before_charge(self) -> None:
        result = summary([[(0., 10.)], [(0., None)], [(0., None)]], 20., 680.)
        assert result['left'] == {'on_periods': [], 'on_time_s': 0.}

    def test_still_recording(self) -> None:
        assert summary([[(0., None)], [], []], 20., None)['left'] == {'on_periods': [{'on': 0., 'off': None}], 'on_time_s': 0.}
