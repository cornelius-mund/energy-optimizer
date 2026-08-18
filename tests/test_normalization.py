"""Tests for shared provider normalization helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    is_fresh,
    validate_hourly_period,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_is_fresh_applies_strict_age_and_expiry_boundaries() -> None:
    assert is_fresh(
        START,
        3600,
        now=START + timedelta(seconds=3599),
        error_factory=ValueError,
        now_message="now must be aware",
    )
    assert not is_fresh(
        START,
        3600,
        now=START + timedelta(hours=1),
        error_factory=ValueError,
        now_message="now must be aware",
    )
    assert not is_fresh(
        START,
        None,
        expires_at=START + timedelta(hours=1),
        now=START + timedelta(hours=1),
        error_factory=ValueError,
        now_message="now must be aware",
    )


def test_is_fresh_skips_time_validation_when_no_constraint_is_configured() -> None:
    assert is_fresh(
        datetime(2026, 1, 1),
        None,
        error_factory=ValueError,
        now_message="now must be aware",
    )


def test_is_fresh_reports_naive_times_with_the_configured_errors() -> None:
    with pytest.raises(ValueError, match="now must be aware"):
        is_fresh(
            START,
            3600,
            now=datetime(2026, 1, 1),
            error_factory=ValueError,
            now_message="now must be aware",
        )

    with pytest.raises(ValueError, match="observation must be aware"):
        is_fresh(
            datetime(2026, 1, 1),
            3600,
            now=START,
            error_factory=ValueError,
            now_message="now must be aware",
            observed_message="observation must be aware",
        )


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (
            START + timedelta(minutes=30),
            START + timedelta(hours=1),
            "start must be aligned",
        ),
        (START, START + timedelta(minutes=30), "end must be aligned"),
        (START + timedelta(hours=1), START, "end must be after start"),
    ],
)
def test_validate_hourly_period_rejects_invalid_boundaries(
    start: datetime, end: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_hourly_period(
            start,
            end,
            error_factory=ValueError,
            start_message="start must be aligned",
            end_message="end must be aligned",
            order_message="end must be after start",
        )


def test_validate_hourly_period_checks_whole_hour_duration() -> None:
    with pytest.raises(ValueError, match="whole hours"):
        validate_hourly_period(
            START,
            START + timedelta(hours=1, minutes=30),
            error_factory=ValueError,
            start_message="start must be aligned",
            end_message="end must be aligned",
            order_message="end must be after start",
            whole_hours_message="period must contain whole hours",
        )


def test_validate_hourly_period_allows_an_open_ended_aligned_period() -> None:
    validate_hourly_period(
        START,
        None,
        error_factory=ValueError,
        start_message="start must be aligned",
        end_message="end must be aligned",
        order_message="end must be after start",
    )


def test_align_to_next_hour_preserves_aligned_values_and_rounds_forward() -> None:
    assert align_to_next_hour(START) == START
    assert align_to_next_hour(START + timedelta(minutes=1)) == START + timedelta(
        hours=1
    )
    assert align_to_next_hour(
        START + timedelta(minutes=59, seconds=59, microseconds=1)
    ) == START + timedelta(hours=1)
