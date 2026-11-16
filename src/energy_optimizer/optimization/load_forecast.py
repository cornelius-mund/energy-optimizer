"""Household-load forecasts for the planning horizon, chosen by backtesting.

A method forecasts every hour of day it has enough samples for. An hour of day
it lacks, for example one that a short meter dropout excludes every night, is
filled from the mean of that hour over four weeks, or of its nearest hours, so
one bad hour never discards a method and its backtested error.
"""

import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from energy_optimizer.optimization.problem import Kilowatt
from energy_optimizer.providers.interfaces import HouseholdLoadData

logger = logging.getLogger(__name__)

ONE_HOUR = timedelta(hours=1)
HOURS_PER_DAY = 24
SEVEN_DAY_MEAN = "seven_day_mean"
WEEKDAY_PROFILE = "weekday_profile"
AUTO = "auto"
SEVEN_DAY_WINDOW = 7
WEEKDAY_PROFILE_WINDOW = 28
GAP_FILL_WINDOW = 28
MINIMUM_SAMPLES_PER_PROFILE_HOUR = 2
SATURDAY = 5

type LocalHour = int


class LoadForecastError(RuntimeError):
    """Raised when the history cannot support a forecast."""


@dataclass(frozen=True)
class HourlyForecast:
    """Forecast load per planned hour and the local hours filled from the
    longer window because the method had no sample for them."""

    load_kw: tuple[Kilowatt, ...]
    filled_local_hours: frozenset[LocalHour] = frozenset()


type Forecaster = Callable[
    [HouseholdLoadData, list[datetime], ZoneInfo], HourlyForecast
]


@dataclass(frozen=True)
class LoadForecast:
    method: str
    load_kw: tuple[Kilowatt, ...]
    mean_absolute_error_kw: float | None


def _recent_samples(
    history: HouseholdLoadData, days: int
) -> Iterator[tuple[datetime, float]]:
    """Yield (hour start, kW) of the valid hours in the last ``days`` days."""
    first_index = max(0, len(history.load_kw) - days * HOURS_PER_DAY)
    for index in range(first_index, len(history.load_kw)):
        load_kw = history.load_kw[index]
        if load_kw is not None:
            yield history.start_time + ONE_HOUR * index, load_kw


def _mean(values: list[float]) -> float:
    return math.fsum(values) / len(values)


def _circular_hour_distance(first: LocalHour, second: LocalHour) -> int:
    difference = abs(first - second)
    return min(difference, HOURS_PER_DAY - difference)


def _fill_from_neighbouring_hours(
    means: dict[LocalHour, float],
) -> dict[LocalHour, float]:
    """Give every hour of day without a sample the mean of its nearest hours."""
    filled = dict(means)
    for hour in range(HOURS_PER_DAY):
        if hour in means:
            continue
        nearest_distance = min(_circular_hour_distance(hour, known) for known in means)
        filled[hour] = _mean(
            [
                means[known]
                for known in means
                if _circular_hour_distance(hour, known) == nearest_distance
            ]
        )
    return filled


def gap_profile(
    history: HouseholdLoadData, time_zone: ZoneInfo
) -> dict[LocalHour, float]:
    """Mean of each local hour over four weeks, gaps from neighbouring hours."""
    samples_by_hour: dict[LocalHour, list[float]] = defaultdict(list)
    for hour_start, load_kw in _recent_samples(history, GAP_FILL_WINDOW):
        samples_by_hour[hour_start.astimezone(time_zone).hour].append(load_kw)
    if not samples_by_hour:
        raise LoadForecastError(
            f"the household-load history has no valid sample in the last "
            f"{GAP_FILL_WINDOW} days"
        )
    return _fill_from_neighbouring_hours(
        {hour: _mean(samples) for hour, samples in samples_by_hour.items()}
    )


def _forecast_with_gaps_filled(
    history: HouseholdLoadData,
    hour_starts: list[datetime],
    time_zone: ZoneInfo,
    own_forecast: Callable[[datetime], float | None],
) -> HourlyForecast:
    """Use the method's own value where it has one; fill the rest."""
    profile: dict[LocalHour, float] | None = None
    filled: set[LocalHour] = set()
    load_kw: list[Kilowatt] = []
    for hour_start in hour_starts:
        value = own_forecast(hour_start)
        if value is None:
            if profile is None:
                profile = gap_profile(history, time_zone)
            local_hour = hour_start.astimezone(time_zone).hour
            filled.add(local_hour)
            value = profile[local_hour]
        load_kw.append(value)
    return HourlyForecast(tuple(load_kw), frozenset(filled))


def seven_day_mean_forecast(
    history: HouseholdLoadData, hour_starts: list[datetime], time_zone: ZoneInfo
) -> HourlyForecast:
    """Mean of the same local hour over the last seven days.

    Hours excluded from the history do not contribute.
    """
    samples_by_hour: dict[LocalHour, list[float]] = defaultdict(list)
    for hour_start, load_kw in _recent_samples(history, SEVEN_DAY_WINDOW):
        samples_by_hour[hour_start.astimezone(time_zone).hour].append(load_kw)

    def own_forecast(hour_start: datetime) -> float | None:
        samples = samples_by_hour.get(hour_start.astimezone(time_zone).hour)
        return _mean(samples) if samples else None

    return _forecast_with_gaps_filled(history, hour_starts, time_zone, own_forecast)


def _is_weekend(moment: datetime, time_zone: ZoneInfo) -> bool:
    return moment.astimezone(time_zone).weekday() >= SATURDAY


def weekday_profile_forecast(
    history: HouseholdLoadData, hour_starts: list[datetime], time_zone: ZoneInfo
) -> HourlyForecast:
    """Mean of the same local hour on the same kind of day (weekday or weekend).

    Four weeks of history give the profile at least eight samples per weekday
    hour and four per weekend hour; with fewer than two the hour is filled.
    """
    samples: dict[tuple[bool, LocalHour], list[float]] = defaultdict(list)
    for hour_start, load_kw in _recent_samples(history, WEEKDAY_PROFILE_WINDOW):
        local = hour_start.astimezone(time_zone)
        samples[(_is_weekend(hour_start, time_zone), local.hour)].append(load_kw)

    def own_forecast(hour_start: datetime) -> float | None:
        kind_of_day = _is_weekend(hour_start, time_zone)
        values = samples.get((kind_of_day, hour_start.astimezone(time_zone).hour), [])
        return (
            _mean(values) if len(values) >= MINIMUM_SAMPLES_PER_PROFILE_HOUR else None
        )

    return _forecast_with_gaps_filled(history, hour_starts, time_zone, own_forecast)


FORECASTERS: dict[str, Forecaster] = {
    SEVEN_DAY_MEAN: seven_day_mean_forecast,
    WEEKDAY_PROFILE: weekday_profile_forecast,
}


def backtest_mean_absolute_error_kw(
    history: HouseholdLoadData,
    forecaster: Forecaster,
    time_zone: ZoneInfo,
    days: int,
) -> float:
    """Forecast each of the last ``days`` days from the history before it."""
    history_end = history.start_time + ONE_HOUR * len(history.load_kw)
    errors: list[float] = []
    for day in range(1, days + 1):
        window_start = history_end - timedelta(days=day)
        cut = int((window_start - history.start_time) / ONE_HOUR)
        if cut <= 0:
            break
        known = replace(history, load_kw=history.load_kw[:cut])
        hours = [window_start + ONE_HOUR * i for i in range(HOURS_PER_DAY)]
        try:
            predicted = forecaster(known, hours, time_zone).load_kw
        except LoadForecastError:
            continue
        actual = history.load_kw[cut : cut + HOURS_PER_DAY]
        errors.extend(
            abs(forecast - observed)
            for forecast, observed in zip(predicted, actual, strict=False)
            if observed is not None
        )
    if not errors:
        raise LoadForecastError("the history is too short to backtest the forecast")
    return math.fsum(errors) / len(errors)


def _ranked_methods(
    history: HouseholdLoadData, time_zone: ZoneInfo, method: str, backtest_days: int
) -> list[tuple[float | None, str]]:
    """Candidate methods by backtest error; unscored when none can be tested."""
    candidates = list(FORECASTERS) if method == AUTO else [method]
    scored: list[tuple[float, str]] = []
    for name in candidates:
        try:
            error = backtest_mean_absolute_error_kw(
                history, FORECASTERS[name], time_zone, backtest_days
            )
        except LoadForecastError:
            continue
        scored.append((error, name))
    if scored:
        return [(error, name) for error, name in sorted(scored)]
    return [(None, SEVEN_DAY_MEAN if method == AUTO else method)]


def forecast_load(
    history: HouseholdLoadData,
    hour_starts: list[datetime],
    time_zone: ZoneInfo,
    method: str = AUTO,
    backtest_days: int = 7,
) -> LoadForecast:
    """Forecast the load with ``method``, or with the method that backtests best."""
    mean_error, chosen = _ranked_methods(history, time_zone, method, backtest_days)[0]
    forecast = FORECASTERS[chosen](history, hour_starts, time_zone)
    if forecast.filled_local_hours:
        logger.warning(
            "event=load_forecast_gaps_filled component=optimization operation=plan "
            "method=%s filled_local_hours=%s",
            chosen,
            ",".join(str(hour) for hour in sorted(forecast.filled_local_hours)),
        )
    logger.info(
        "event=load_forecast_selected component=optimization operation=plan "
        "method=%s mean_absolute_error_kw=%s",
        chosen,
        "unknown" if mean_error is None else f"{mean_error:.3f}",
    )
    return LoadForecast(chosen, forecast.load_kw, mean_error)
