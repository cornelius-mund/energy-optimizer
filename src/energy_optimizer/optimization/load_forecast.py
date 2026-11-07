"""Household-load forecasts for the planning horizon, chosen by backtesting."""

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
FILLED_PROFILE = "filled_profile"
AUTO = "auto"
SEVEN_DAY_WINDOW = 7
WEEKDAY_PROFILE_WINDOW = 28
MINIMUM_SAMPLES_PER_PROFILE_HOUR = 2
SATURDAY = 5


class LoadForecastError(RuntimeError):
    """Raised when the history cannot support a forecast."""


type Forecaster = Callable[
    [HouseholdLoadData, list[datetime], ZoneInfo], tuple[Kilowatt, ...]
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


def seven_day_mean_forecast(
    history: HouseholdLoadData, hour_starts: list[datetime], time_zone: ZoneInfo
) -> tuple[Kilowatt, ...]:
    """Mean of the same local hour over the last seven days.

    Hours excluded from the history do not contribute; an hour of day without
    any sample is an error.
    """
    samples_by_hour: dict[int, list[float]] = defaultdict(list)
    for hour_start, load_kw in _recent_samples(history, SEVEN_DAY_WINDOW):
        samples_by_hour[hour_start.astimezone(time_zone).hour].append(load_kw)
    forecast: list[Kilowatt] = []
    for hour_start in hour_starts:
        local_hour = hour_start.astimezone(time_zone).hour
        samples = samples_by_hour.get(local_hour)
        if not samples:
            raise LoadForecastError(
                f"the household-load history has no sample for local hour "
                f"{local_hour} in the last {SEVEN_DAY_WINDOW} days"
            )
        forecast.append(math.fsum(samples) / len(samples))
    return tuple(forecast)


def _is_weekend(moment: datetime, time_zone: ZoneInfo) -> bool:
    return moment.astimezone(time_zone).weekday() >= SATURDAY


def weekday_profile_forecast(
    history: HouseholdLoadData, hour_starts: list[datetime], time_zone: ZoneInfo
) -> tuple[Kilowatt, ...]:
    """Mean of the same local hour on the same kind of day (weekday or weekend).

    Four weeks of history give the profile at least eight samples per weekday
    hour and four per weekend hour; fewer than two is an error.
    """
    samples: dict[tuple[bool, int], list[float]] = defaultdict(list)
    for hour_start, load_kw in _recent_samples(history, WEEKDAY_PROFILE_WINDOW):
        local = hour_start.astimezone(time_zone)
        samples[(_is_weekend(hour_start, time_zone), local.hour)].append(load_kw)
    forecast: list[Kilowatt] = []
    for hour_start in hour_starts:
        key = (
            _is_weekend(hour_start, time_zone),
            hour_start.astimezone(time_zone).hour,
        )
        values = samples.get(key, [])
        if len(values) < MINIMUM_SAMPLES_PER_PROFILE_HOUR:
            kind = "weekend" if key[0] else "weekday"
            raise LoadForecastError(
                f"the household-load history has fewer than "
                f"{MINIMUM_SAMPLES_PER_PROFILE_HOUR} samples for {kind} local hour "
                f"{key[1]} in the last {WEEKDAY_PROFILE_WINDOW} days"
            )
        forecast.append(math.fsum(values) / len(values))
    return tuple(forecast)


def _circular_hour_distance(first: int, second: int) -> int:
    difference = abs(first - second)
    return min(difference, HOURS_PER_DAY - difference)


def _fill_from_neighbouring_hours(means: dict[int, float]) -> dict[int, float]:
    """Give every hour of day without a sample the mean of its nearest hours."""
    filled = dict(means)
    for hour in range(HOURS_PER_DAY):
        if hour in means:
            continue
        nearest_distance = min(_circular_hour_distance(hour, known) for known in means)
        nearest = [
            means[known]
            for known in means
            if _circular_hour_distance(hour, known) == nearest_distance
        ]
        filled[hour] = math.fsum(nearest) / len(nearest)
    return filled


def filled_profile_forecast(
    history: HouseholdLoadData, hour_starts: list[datetime], time_zone: ZoneInfo
) -> tuple[Kilowatt, ...]:
    """Mean of the same local hour over four weeks, gaps from neighbouring hours.

    The last resort when no backtested method can forecast every planned hour:
    an hour of day without any valid sample (for example one that is excluded
    every day) must not stop planning.
    """
    samples_by_hour: dict[int, list[float]] = defaultdict(list)
    for hour_start, load_kw in _recent_samples(history, WEEKDAY_PROFILE_WINDOW):
        samples_by_hour[hour_start.astimezone(time_zone).hour].append(load_kw)
    if not samples_by_hour:
        raise LoadForecastError(
            f"the household-load history has no valid sample in the last "
            f"{WEEKDAY_PROFILE_WINDOW} days"
        )
    means = {
        hour: math.fsum(samples) / len(samples)
        for hour, samples in samples_by_hour.items()
    }
    missing_hours = sorted(set(range(HOURS_PER_DAY)) - means.keys())
    if missing_hours:
        logger.warning(
            "event=load_forecast_gaps_filled component=optimization operation=plan "
            "missing_local_hours=%s",
            ",".join(str(hour) for hour in missing_hours),
        )
    profile = _fill_from_neighbouring_hours(means)
    return tuple(
        profile[hour_start.astimezone(time_zone).hour] for hour_start in hour_starts
    )


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
            predicted = forecaster(known, hours, time_zone)
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


def forecast_load(
    history: HouseholdLoadData,
    hour_starts: list[datetime],
    time_zone: ZoneInfo,
    method: str = AUTO,
    backtest_days: int = 7,
) -> LoadForecast:
    """Forecast the load with ``method``, or with the method that backtests best."""
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
    ranked: list[tuple[float | None, str]] = [
        (error, name) for error, name in sorted(scored)
    ]
    if not ranked:
        ranked = [(None, SEVEN_DAY_MEAN if method == AUTO else method)]
    failure: LoadForecastError | None = None
    for mean_error, chosen in ranked:
        try:
            load_kw = FORECASTERS[chosen](history, hour_starts, time_zone)
        except LoadForecastError as error:
            failure = error
            continue
        logger.info(
            "event=load_forecast_selected component=optimization operation=plan "
            "method=%s mean_absolute_error_kw=%s",
            chosen,
            "unknown" if mean_error is None else f"{mean_error:.3f}",
        )
        return LoadForecast(chosen, load_kw, mean_error)
    assert failure is not None
    logger.warning(
        "event=load_forecast_fallback component=optimization operation=plan "
        "method=%s reason=%s",
        FILLED_PROFILE,
        failure,
    )
    return LoadForecast(
        FILLED_PROFILE, filled_profile_forecast(history, hour_starts, time_zone), None
    )
