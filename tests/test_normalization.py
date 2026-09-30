"""Tests for shared provider normalization helpers."""

from datetime import datetime, timedelta, timezone
from functools import partial

import pytest

from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    is_fresh,
    validate_hourly_period,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
NAIVE = datetime(2026, 1, 1)

fresh = partial(is_fresh, error_factory=ValueError, now_message="now must be aware")
validate_period = partial(
    validate_hourly_period,
    error_factory=ValueError,
    start_message="start must be aligned",
    end_message="end must be aligned",
    order_message="end must be after start",
)


def test_is_fresh_applies_strict_age_and_expiry_boundaries() -> None:
    assert fresh(START, 3600, now=START + timedelta(seconds=3599))
    assert not fresh(START, 3600, now=START + HOUR)
    assert not fresh(START, None, expires_at=START + HOUR, now=START + HOUR)


def test_is_fresh_skips_time_validation_when_no_constraint_is_configured() -> None:
    assert fresh(NAIVE, None)


def test_is_fresh_reports_naive_times_with_the_configured_errors() -> None:
    with pytest.raises(ValueError, match="now must be aware"):
        fresh(START, 3600, now=NAIVE)

    with pytest.raises(ValueError, match="observation must be aware"):
        fresh(NAIVE, 3600, now=START, observed_message="observation must be aware")


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (START + timedelta(minutes=30), START + HOUR, "start must be aligned"),
        (START, START + timedelta(minutes=30), "end must be aligned"),
        (START + HOUR, START, "end must be after start"),
    ],
)
def test_validate_hourly_period_rejects_invalid_boundaries(
    start: datetime, end: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_period(start, end)


def test_validate_hourly_period_checks_whole_hour_duration() -> None:
    with pytest.raises(ValueError, match="whole hours"):
        validate_period(
            START,
            START + timedelta(hours=1, minutes=30),
            whole_hours_message="period must contain whole hours",
        )


def test_validate_hourly_period_allows_an_open_ended_aligned_period() -> None:
    validate_period(START, None)


def test_align_to_next_hour_preserves_aligned_values_and_rounds_forward() -> None:
    assert align_to_next_hour(START) == START
    assert align_to_next_hour(START + timedelta(minutes=1)) == START + HOUR
    just_after = START + timedelta(minutes=59, seconds=59, microseconds=1)
    assert align_to_next_hour(just_after) == START + HOUR
