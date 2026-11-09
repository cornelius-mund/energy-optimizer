"""Weekly driving and plug-in patterns of the car, learned from its history."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import fmean
from zoneinfo import ZoneInfo

from energy_optimizer.providers.interfaces import EvHistoryData

ONE_HOUR = timedelta(hours=1)
MINIMUM_KNOWN_HOURS_PER_DAY = 20
MINIMUM_SAMPLES_PER_HOUR = 2
PLUGGED_IN_SHARE_THRESHOLD = 0.5
# Without an arrival on a day, its driving is deducted in the early evening.
FALLBACK_RETURN_TIME = time(18)

type Weekday = int
type WeekdayHour = tuple[Weekday, int]


@dataclass(frozen=True)
class EvPatterns:
    """Average driving energy per local weekday and plug-in share per weekday hour."""

    consumption_kwh_by_weekday: dict[Weekday, float]
    plugged_in_share: dict[WeekdayHour, float]

    def is_empty(self) -> bool:
        return not self.consumption_kwh_by_weekday and not self.plugged_in_share

    def expected_plugged_in(self, moment: datetime) -> bool | None:
        share = self.plugged_in_share.get((moment.weekday(), moment.hour))
        return None if share is None else share >= PLUGGED_IN_SHARE_THRESHOLD


NO_PATTERNS = EvPatterns(consumption_kwh_by_weekday={}, plugged_in_share={})


def learn_ev_patterns(
    history: EvHistoryData, capacity_kwh: float, time_zone: ZoneInfo
) -> EvPatterns:
    """Driving energy is the sum of the state-of-charge drops of a local day."""
    local_hours = [
        (history.start_time + ONE_HOUR * index).astimezone(time_zone)
        for index in range(len(history.state_of_charge_percent))
    ]
    return EvPatterns(
        consumption_kwh_by_weekday=_consumption_by_weekday(
            local_hours, history.state_of_charge_percent, capacity_kwh
        ),
        plugged_in_share=_plugged_in_share(local_hours, history.plugged_in),
    )


def _consumption_by_weekday(
    local_hours: list[datetime],
    state_of_charge_percent: tuple[float | None, ...],
    capacity_kwh: float,
) -> dict[Weekday, float]:
    drops_by_day: dict[date, list[float]] = defaultdict(list)
    previous: float | None = None
    for moment, percent in zip(local_hours, state_of_charge_percent, strict=True):
        if percent is not None and previous is not None:
            drops_by_day[moment.date()].append(max(previous - percent, 0.0))
        previous = percent
    daily_kwh_by_weekday: dict[Weekday, list[float]] = defaultdict(list)
    for day, drops in drops_by_day.items():
        if len(drops) >= MINIMUM_KNOWN_HOURS_PER_DAY:
            daily_kwh_by_weekday[day.weekday()].append(sum(drops) / 100 * capacity_kwh)
    return {weekday: fmean(days) for weekday, days in daily_kwh_by_weekday.items()}


def _plugged_in_share(
    local_hours: list[datetime], plugged_in: tuple[bool | None, ...]
) -> dict[WeekdayHour, float]:
    samples: dict[WeekdayHour, list[float]] = defaultdict(list)
    for moment, plugged in zip(local_hours, plugged_in, strict=True):
        if plugged is not None:
            samples[(moment.weekday(), moment.hour)].append(float(plugged))
    return {
        weekday_hour: fmean(values)
        for weekday_hour, values in samples.items()
        if len(values) >= MINIMUM_SAMPLES_PER_HOUR
    }


def expected_plugged_in(
    patterns: EvPatterns, local_hours: list[datetime], plugged_in_now: bool
) -> tuple[bool, ...]:
    """The current plug state holds until the learned pattern next changes state;
    afterwards the pattern decides, and hours it does not know keep the current
    state.
    """
    learned = [patterns.expected_plugged_in(moment) for moment in local_hours]
    expected = [plugged_in_now if value is None else value for value in learned]
    first_change = next(
        (
            index
            for index in range(1, len(expected))
            if expected[index] != expected[index - 1]
        ),
        len(expected),
    )
    return tuple([plugged_in_now] * first_change + expected[first_change:])


def driving_consumption(
    patterns: EvPatterns, local_hours: list[datetime], plugged_in: tuple[bool, ...]
) -> tuple[float, ...]:
    """Deduct each day's driving energy when the car comes back that day: at its
    first arrival, else at the fallback return time, if that hour is planned.
    """
    consumption = [0.0] * len(local_hours)
    days = sorted({moment.date() for moment in local_hours})
    for day in days:
        energy_kwh = patterns.consumption_kwh_by_weekday.get(day.weekday(), 0.0)
        if energy_kwh <= 0:
            continue
        return_index = _return_hour(local_hours, plugged_in, day)
        if return_index is not None:
            consumption[return_index] += energy_kwh
    return tuple(consumption)


def _return_hour(
    local_hours: list[datetime], plugged_in: tuple[bool, ...], day: date
) -> int | None:
    for index in range(1, len(local_hours)):
        arrives = plugged_in[index] and not plugged_in[index - 1]
        if arrives and local_hours[index].date() == day:
            return index
    for index, moment in enumerate(local_hours):
        if index > 0 and moment.date() == day and moment.time() == FALLBACK_RETURN_TIME:
            return index
    return None
