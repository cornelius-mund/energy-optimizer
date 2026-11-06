"""Load forecast methods and the backtest that chooses between them."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from energy_optimizer.config import Configuration
from energy_optimizer.optimization.load_forecast import (
    SEVEN_DAY_MEAN,
    WEEKDAY_PROFILE,
    LoadForecastError,
    backtest_mean_absolute_error_kw,
    forecast_load,
    seven_day_mean_forecast,
    weekday_profile_forecast,
)
from energy_optimizer.optimization.plan_generation import PlanGenerator
from energy_optimizer.providers.interfaces import HouseholdLoadData, SourceMetadata
from energy_optimizer.storage import ProviderDataStore
from test_plan_generation import prices, pv_forecast, snapshot

UTC_ZONE = ZoneInfo("UTC")
MONDAY = datetime(2026, 2, 2, tzinfo=UTC)
WEEKDAY_KW = 1.0
WEEKEND_KW = 3.0
WEEKS = 5


def weekly_history(weeks: int = WEEKS, days: int | None = None) -> HouseholdLoadData:
    """Constant 1 kW on weekdays and 3 kW on weekends, starting on a Monday."""
    values = tuple(
        WEEKEND_KW if (hour // 24) % 7 >= 5 else WEEKDAY_KW
        for hour in range((days if days is not None else weeks * 7) * 24)
    )
    return HouseholdLoadData(
        schema_version="1",
        start_time=MONDAY,
        interval_minutes=60,
        load_kw=values,
        unit="kW",
        source=SourceMetadata(provider="test"),
        retrieved_at=MONDAY,
        latest_observation_at=MONDAY,
    )


def day_hours(history: HouseholdLoadData, day_offset: int) -> list[datetime]:
    end = history.start_time + timedelta(hours=len(history.load_kw))
    return [end + timedelta(days=day_offset, hours=hour) for hour in range(24)]


def test_the_history_ends_on_a_monday_so_the_next_day_is_a_weekday() -> None:
    history = weekly_history()

    assert day_hours(history, 0)[0].weekday() == 0


def test_the_weekday_profile_predicts_a_weekday_and_a_weekend_day_exactly() -> None:
    history = weekly_history()

    monday = weekday_profile_forecast(history, day_hours(history, 0), UTC_ZONE)
    saturday = weekday_profile_forecast(history, day_hours(history, 5), UTC_ZONE)

    assert monday == pytest.approx((WEEKDAY_KW,) * 24)
    assert saturday == pytest.approx((WEEKEND_KW,) * 24)


def test_the_seven_day_mean_cannot_tell_weekdays_from_weekends() -> None:
    history = weekly_history()
    expected = (5 * WEEKDAY_KW + 2 * WEEKEND_KW) / 7

    monday = seven_day_mean_forecast(history, day_hours(history, 0), UTC_ZONE)

    assert monday == pytest.approx((expected,) * 24)


def test_the_weekday_profile_beats_the_seven_day_mean_in_the_backtest() -> None:
    history = weekly_history()

    weekday_error = backtest_mean_absolute_error_kw(
        history, weekday_profile_forecast, UTC_ZONE, 7
    )
    mean_error = backtest_mean_absolute_error_kw(
        history, seven_day_mean_forecast, UTC_ZONE, 7
    )

    assert weekday_error == pytest.approx(0.0, abs=1e-9)
    assert mean_error > 0.5


def test_auto_picks_the_method_with_the_smaller_backtest_error() -> None:
    history = weekly_history()

    forecast = forecast_load(history, day_hours(history, 5), UTC_ZONE)

    assert forecast.method == WEEKDAY_PROFILE
    assert forecast.mean_absolute_error_kw == pytest.approx(0.0, abs=1e-9)
    assert forecast.load_kw == pytest.approx((WEEKEND_KW,) * 24)


def test_a_configured_method_is_used_whatever_the_backtest_says() -> None:
    history = weekly_history()

    forecast = forecast_load(
        history, day_hours(history, 0), UTC_ZONE, method=SEVEN_DAY_MEAN
    )

    assert forecast.method == SEVEN_DAY_MEAN
    assert forecast.mean_absolute_error_kw is not None


def test_auto_falls_back_when_the_best_method_cannot_forecast_the_day() -> None:
    history = weekly_history(days=5)

    forecast = forecast_load(history, day_hours(history, 0), UTC_ZONE)

    assert forecast.method == SEVEN_DAY_MEAN
    assert forecast.load_kw == pytest.approx((WEEKDAY_KW,) * 24)


def test_the_weekday_profile_needs_samples_for_the_kind_of_day() -> None:
    history = weekly_history(days=5)

    with pytest.raises(LoadForecastError, match="weekend local hour 0"):
        weekday_profile_forecast(history, day_hours(history, 0), UTC_ZONE)


def test_a_history_too_short_to_backtest_is_reported() -> None:
    history = HouseholdLoadData(**{**weekly_history().__dict__, "load_kw": (1.0,) * 12})

    with pytest.raises(LoadForecastError, match="too short"):
        backtest_mean_absolute_error_kw(history, seven_day_mean_forecast, UTC_ZONE, 7)


def test_excluded_hours_are_ignored_in_the_backtest() -> None:
    history = weekly_history()
    values: list[float | None] = list(history.load_kw)
    values[-5] = None
    gap = HouseholdLoadData(**{**history.__dict__, "load_kw": tuple(values)})

    error = backtest_mean_absolute_error_kw(gap, weekday_profile_forecast, UTC_ZONE, 7)

    assert error == pytest.approx(0.0, abs=1e-9)


def planning_load(tmp_path: Path, method: str) -> tuple[float, ...]:
    configuration = Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "timezone": "UTC",
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "load_forecast": {"method": method},
        }
    )
    return (
        PlanGenerator(configuration, ProviderDataStore(tmp_path))
        .planning_problem(
            snapshot(
                household_load=weekly_history(),
                pv_generation=pv_forecast([0.0] * 6),
                electricity_prices=prices([0.3] * 6),
            )
        )
        .load_kw
    )


def test_the_plan_uses_the_forecast_method_that_the_configuration_selects(
    tmp_path: Path,
) -> None:
    # The plan starts on a Tuesday, a weekday.
    assert planning_load(tmp_path, "auto") == pytest.approx((WEEKDAY_KW,) * 6)
    assert planning_load(tmp_path, SEVEN_DAY_MEAN) == pytest.approx(
        ((5 * WEEKDAY_KW + 2 * WEEKEND_KW) / 7,) * 6
    )
