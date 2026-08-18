"""Tests for dashboard hourly-series alignment."""

from datetime import datetime, timedelta, timezone

from energy_optimizer.api.series import align_hourly_values

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_align_hourly_values_returns_half_open_range_with_nullable_gaps() -> None:
    values = {
        START: 1.0,
        START + timedelta(hours=2): 3.0,
    }

    assert align_hourly_values(values, START, START + timedelta(hours=4)) == [
        (START, 1.0),
        (START + timedelta(hours=1), None),
        (START + timedelta(hours=2), 3.0),
        (START + timedelta(hours=3), None),
    ]


def test_align_hourly_values_returns_no_values_for_an_empty_range() -> None:
    assert align_hourly_values({START: 1.0}, START, START) == []
