import math
from dataclasses import dataclass

from apps.learning.models import HazardVoidReason

DUPLICATE_WITHIN_SECONDS = 0.05
LATE_CLICK_GRACE_SECONDS = 2.0

ZONE_OPENS_BEFORE_SECONDS = 1.0
ZONE_CLOSES_AFTER_SECONDS = 1.5

PATTERN_MINIMUM_CLICKS = 5
PATTERN_SHORTEST_INTERVAL_SECONDS = 0.3
PATTERN_LONGEST_INTERVAL_SECONDS = 3.0
PATTERN_MINIMUM_SPAN_SECONDS = 3.0
PATTERN_MAXIMUM_VARIATION = 0.15

BURST_LIMITS = ((1.0, 4), (2.0, 7))

SPREAD_SECTION_SECONDS = 5.0
SPREAD_MINIMUM_COVERAGE = 0.8
SPREAD_MINIMUM_SHARE_OUTSIDE_ZONES = 0.6

TOLERANCE = 1e-9

Zone = tuple[float, float]


@dataclass(frozen=True)
class Verdict:
    clicks: tuple[float, ...]
    scorable: tuple[float, ...]
    reason: HazardVoidReason | None

    @property
    def voided(self) -> bool:
        return self.reason is not None


def zones_for(windows) -> list[Zone]:
    return [
        (float(window.starts_at) - ZONE_OPENS_BEFORE_SECONDS, float(window.ends_at) + ZONE_CLOSES_AFTER_SECONDS)
        for window in windows
    ]


def deduplicated(clicks: list[float]) -> list[float]:
    kept: list[float] = []
    for moment in clicks:
        if kept and moment - kept[-1] < DUPLICATE_WITHIN_SECONDS:
            continue
        kept.append(moment)
    return kept


def in_time(clicks: list[float], windows) -> list[float]:
    cutoff = late_click_cutoff(windows)
    if cutoff is None:
        return list(clicks)
    return [moment for moment in clicks if moment <= cutoff + TOLERANCE]


def late_click_cutoff(windows) -> float | None:
    ends = [float(window.ends_at) for window in windows]
    if not ends:
        return None
    return max(ends) + LATE_CLICK_GRACE_SECONDS


def inspect(clicked_at, windows, *, max_clicks: int, duration_seconds=None) -> Verdict:
    scorable = in_time(sorted(_real_seconds(clicked_at)), windows)
    clicks = deduplicated(scorable)
    zones = zones_for(windows)
    return Verdict(
        clicks=tuple(clicks),
        scorable=tuple(scorable),
        reason=_reason_for(clicks, zones, max_clicks, duration_seconds),
    )


def _reason_for(clicks: list[float], zones: list[Zone], max_clicks: int, duration_seconds) -> HazardVoidReason | None:
    if len(clicks) > max_clicks:
        return HazardVoidReason.TOO_MANY
    if _burst(clicks):
        return HazardVoidReason.BURST
    if _pattern(clicks, zones):
        return HazardVoidReason.PATTERN
    if _spread(clicks, zones, duration_seconds):
        return HazardVoidReason.SPREAD
    return None


def _real_seconds(clicked_at) -> list[float]:
    seconds = []
    for value in clicked_at or []:
        try:
            moment = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(moment) and moment >= 0:
            seconds.append(moment)
    return seconds


def _inside_any_zone(moment: float, zones: list[Zone]) -> bool:
    return any(start <= moment <= end for start, end in zones)


def _within_one_zone(run: list[float], zones: list[Zone]) -> bool:
    return any(start <= run[0] and run[-1] <= end for start, end in zones)


def _burst(clicks: list[float]) -> bool:
    for span, allowed in BURST_LIMITS:
        first = 0
        for last, moment in enumerate(clicks):
            while moment - clicks[first] > span + TOLERANCE:
                first += 1
            if last - first + 1 > allowed:
                return True
    return False


def _pattern(clicks: list[float], zones: list[Zone]) -> bool:
    total = len(clicks)
    for first in range(total - PATTERN_MINIMUM_CLICKS + 1):
        intervals = 0
        summed = 0.0
        summed_squares = 0.0
        for last in range(first + 1, total):
            interval = clicks[last] - clicks[last - 1]
            if not PATTERN_SHORTEST_INTERVAL_SECONDS <= interval <= PATTERN_LONGEST_INTERVAL_SECONDS:
                break
            intervals += 1
            summed += interval
            summed_squares += interval * interval
            if intervals + 1 < PATTERN_MINIMUM_CLICKS:
                continue
            if clicks[last] - clicks[first] < PATTERN_MINIMUM_SPAN_SECONDS:
                continue
            if _variation(intervals, summed, summed_squares) > PATTERN_MAXIMUM_VARIATION:
                continue
            if _within_one_zone(clicks[first : last + 1], zones):
                continue
            return True
    return False


def _variation(intervals: int, summed: float, summed_squares: float) -> float:
    mean = summed / intervals
    if mean <= 0:
        return math.inf
    variance = max(summed_squares / intervals - mean * mean, 0.0)
    return math.sqrt(variance) / mean


def _spread(clicks: list[float], zones: list[Zone], duration_seconds) -> bool:
    if not clicks or duration_seconds is None:
        return False

    duration = float(duration_seconds)
    if duration <= 0:
        return False

    sections = math.ceil(duration / SPREAD_SECTION_SECONDS)
    occupied = {min(int(moment / SPREAD_SECTION_SECONDS), sections - 1) for moment in clicks}
    if len(occupied) / sections < SPREAD_MINIMUM_COVERAGE:
        return False

    outside = sum(1 for moment in clicks if not _inside_any_zone(moment, zones))
    return outside / len(clicks) >= SPREAD_MINIMUM_SHARE_OUTSIDE_ZONES
