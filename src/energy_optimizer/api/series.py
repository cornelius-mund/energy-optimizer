"""Shared timestamp alignment for dashboard forecast series."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TypeVar

ValueT = TypeVar("ValueT")


def align_hourly_values(
    values_by_timestamp: dict[datetime, ValueT],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, ValueT | None]]:
    """Return one nullable value for every requested hourly timestamp."""
    timestamps = [
        start + timedelta(hours=index)
        for index in range(int((end - start).total_seconds() // 3600))
    ]
    return [(timestamp, values_by_timestamp.get(timestamp)) for timestamp in timestamps]
