import pytest

from apps.learning import hazard_detection
from apps.learning.models import HazardVoidReason, HazardWindow

HAZARD = HazardWindow(label="Cyclist pulls out", starts_at=10, ends_at=15)
LATE_HAZARD = HazardWindow(label="Lorry reverses", starts_at=50, ends_at=55)
SECOND_HAZARD = HazardWindow(label="Child runs out", starts_at=24, ends_at=28)

IRREGULAR_INTERVALS = (0.4, 2.9, 1.1, 5.0)


def reason_for(clicks, windows=(HAZARD,), max_clicks=15, duration=None):
    return hazard_detection.inspect(clicks, list(windows), max_clicks=max_clicks, duration_seconds=duration).reason


def accepted(clicks, windows=(HAZARD,)):
    return list(hazard_detection.inspect(clicks, list(windows), max_clicks=15).clicks)


def irregular(count, start=1.0):
    clicks = [start]
    for index in range(count - 1):
        clicks.append(round(clicks[-1] + IRREGULAR_INTERVALS[index % len(IRREGULAR_INTERVALS)], 2))
    return clicks


def steady(count, interval, start=2.0):
    return [round(start + index * interval, 2) for index in range(count)]


class TestPatternClicking:
    def test_five_evenly_spaced_clicks_are_a_pattern(self):
        assert reason_for(steady(5, 1.0)) == HazardVoidReason.PATTERN

    def test_four_evenly_spaced_clicks_are_not(self):
        assert reason_for(steady(4, 1.0)) is None

    def test_intervals_shorter_than_three_tenths_of_a_second_are_not_a_pattern(self):
        assert reason_for(steady(12, 0.29)) is None

    def test_intervals_longer_than_three_seconds_are_not_a_pattern(self):
        assert reason_for(steady(5, 3.5)) is None

    def test_a_pattern_has_to_last_three_seconds(self):
        assert reason_for(steady(5, 0.6)) is None

    def test_uneven_intervals_are_not_a_pattern(self):
        assert reason_for([2.0, 3.0, 4.6, 5.1, 7.0]) is None

    def test_a_run_inside_one_hazards_response_zone_is_left_alone(self):
        assert reason_for([10.0, 11.0, 12.0, 13.0, 14.0]) is None

    def test_a_run_that_outgrows_the_response_zone_is_a_pattern(self):
        assert reason_for([14.0, 15.0, 16.0, 17.0, 18.0], windows=[HAZARD, LATE_HAZARD]) == (HazardVoidReason.PATTERN)

    def test_a_pattern_away_from_every_hazard_is_caught(self):
        assert reason_for(steady(6, 2.0, start=30.0), windows=[HAZARD, LATE_HAZARD]) == HazardVoidReason.PATTERN

    def test_a_steady_run_buried_in_otherwise_normal_clicking_is_caught(self):
        assert reason_for([2.0, 30.0, 31.0, 32.0, 33.0, 34.0], windows=[HAZARD, LATE_HAZARD]) == (
            HazardVoidReason.PATTERN
        )


class TestRapidBursts:
    def test_five_clicks_in_a_second_are_a_burst(self):
        assert reason_for([5.0, 5.2, 5.4, 5.6, 6.0]) == HazardVoidReason.BURST

    def test_four_clicks_in_a_second_are_not(self):
        assert reason_for([5.0, 5.2, 5.4, 5.6]) is None

    def test_eight_clicks_in_two_seconds_are_a_burst(self):
        assert reason_for([5.0, 5.3, 5.6, 5.9, 6.2, 6.5, 6.8, 7.0]) == HazardVoidReason.BURST

    def test_seven_clicks_in_two_seconds_are_not(self):
        assert reason_for([5.0, 5.3, 5.6, 5.9, 6.2, 6.5, 6.8]) is None

    def test_a_burst_straddling_a_whole_second_is_still_a_burst(self):
        assert reason_for([0.9, 1.0, 1.1, 1.2, 1.3]) == HazardVoidReason.BURST

    def test_one_press_registered_six_times_is_not_a_burst(self):
        assert reason_for([5.0, 5.01, 5.02, 5.03, 5.04, 5.05]) is None


class TestSpreadAcrossTheClip:
    def test_clicks_in_every_section_of_the_clip_are_spread(self):
        clicks = [1.0, 6.0, 11.0, 17.0, 22.0, 29.0]

        assert reason_for(clicks, windows=[HAZARD, SECOND_HAZARD], duration=30) == HazardVoidReason.SPREAD

    def test_clicks_in_half_the_sections_are_not(self):
        clicks = [1.0, 6.0, 11.0]

        assert reason_for(clicks, windows=[HAZARD, SECOND_HAZARD], duration=30) is None

    def test_clicks_that_mostly_sit_around_hazards_are_not_spread(self):
        clicks = [1.0, 9.5, 11.0, 16.0, 24.0, 27.0]

        assert reason_for(clicks, windows=[HAZARD, SECOND_HAZARD], duration=30) is None

    def test_the_last_part_section_counts_as_a_section(self):
        clicks = [1.0, 6.0, 11.0]

        assert reason_for(clicks, windows=[], duration=12) == HazardVoidReason.SPREAD

    def test_a_clip_too_short_to_cover_is_not_spread(self):
        clicks = [1.0, 11.0]

        assert reason_for(clicks, windows=[], duration=30) is None

    def test_no_clicks_is_not_spread(self):
        assert reason_for([], windows=[], duration=30) is None

    def test_a_clip_whose_length_is_unknown_is_never_spread(self):
        clicks = [1.0, 6.0, 11.0, 17.0, 22.0, 29.0]

        assert reason_for(clicks, windows=[HAZARD, SECOND_HAZARD], duration=None) is None

    def test_a_length_of_zero_is_treated_as_unknown(self):
        assert reason_for([1.0, 6.0, 11.0], windows=[], duration=0) is None


class TestTheClickLimit:
    def test_fifteen_clicks_are_allowed(self):
        assert reason_for(irregular(15), windows=[HAZARD, LATE_HAZARD]) is None

    def test_sixteen_clicks_are_too_many(self):
        assert reason_for(irregular(16), windows=[HAZARD, LATE_HAZARD]) == HazardVoidReason.TOO_MANY

    def test_a_staff_set_limit_is_what_counts(self):
        assert reason_for(irregular(5), windows=[HAZARD, LATE_HAZARD], max_clicks=4) == HazardVoidReason.TOO_MANY
        assert reason_for(irregular(16), windows=[HAZARD, LATE_HAZARD], max_clicks=20) is None

    def test_near_duplicates_do_not_count_towards_the_limit(self):
        clicks = [*irregular(15), 1.02]

        assert reason_for(clicks, windows=[HAZARD, LATE_HAZARD]) is None

    def test_late_clicks_do_not_count_towards_the_limit(self):
        clicks = [*irregular(15), 57.01, 100.0]

        assert reason_for(clicks, windows=[HAZARD, LATE_HAZARD]) is None


class TestSanitisingWhatWasSubmitted:
    def test_clicks_are_put_in_order(self):
        assert accepted([12.0, 10.5, 11.0]) == [10.5, 11.0, 12.0]

    def test_one_press_recorded_twice_counts_once(self):
        assert accepted([10.5, 10.53]) == [10.5]

    def test_a_chain_of_near_duplicates_is_measured_from_the_click_that_was_kept(self):
        assert accepted([10.5, 10.52, 10.54, 10.56]) == [10.5, 10.56]

    def test_five_hundredths_of_a_second_apart_is_two_clicks(self):
        assert accepted([10.5, 10.55]) == [10.5, 10.55]

    def test_impossible_times_are_dropped(self):
        assert accepted([float("nan"), float("inf"), float("-inf"), -2.0, 10.5]) == [10.5]

    def test_nothing_submitted_is_handled(self):
        assert accepted([]) == []
        assert reason_for([]) is None


class TestLateClicks:
    def test_a_click_two_seconds_after_the_last_hazard_still_counts(self):
        assert accepted([10.5, 17.0]) == [10.5, 17.0]

    def test_a_click_after_that_is_ignored(self):
        assert accepted([10.5, 17.01]) == [10.5]

    def test_the_cutoff_follows_the_last_hazard_of_several(self):
        assert accepted([10.5, 30.0, 57.0, 57.01], windows=[HAZARD, LATE_HAZARD]) == [10.5, 30.0, 57.0]

    def test_a_clip_with_no_hazards_keeps_every_click(self):
        assert accepted([10.5, 400.0], windows=[]) == [10.5, 400.0]


class TestWhichReasonIsReported:
    def test_too_many_clicks_outranks_the_rest(self):
        clicks = steady(20, 0.1, start=10.0)

        assert reason_for(clicks) == HazardVoidReason.TOO_MANY

    def test_a_burst_outranks_a_pattern(self):
        clicks = [20.0, 20.2, 20.4, 20.6, 21.0, 30.0, 31.0, 32.0, 33.0, 34.0]

        assert reason_for(clicks, windows=[HAZARD, LATE_HAZARD]) == HazardVoidReason.BURST

    def test_a_pattern_outranks_spread_clicking(self):
        clicks = steady(15, 2.0, start=1.0)

        assert reason_for(clicks, windows=[SECOND_HAZARD], duration=30) == HazardVoidReason.PATTERN

    def test_clicking_normally_has_no_reason(self):
        assert reason_for([10.5, 11.2], windows=[HAZARD, SECOND_HAZARD], duration=30) is None


@pytest.mark.parametrize("count", [0, 1, 2, 500])
def test_any_number_of_clicks_can_be_inspected(count):
    verdict = hazard_detection.inspect(steady(count, 0.11), [], max_clicks=15)

    assert len(verdict.clicks) == count
    assert verdict.voided is (count > 15)
