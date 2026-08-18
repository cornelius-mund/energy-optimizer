"""Reusable validation helpers for HTTP request models."""

from __future__ import annotations

import math
from datetime import datetime


def replace_non_finite_values(values: object) -> object:
    """Turn non-finite JSON numbers into validation failures instead of crashes."""
    if isinstance(values, list):
        return [
            None if isinstance(value, float) and not math.isfinite(value) else value
            for value in values
        ]
    if isinstance(values, float) and not math.isfinite(values):
        return None
    return values


def require_aware_timestamps(
    values: datetime | list[datetime],
) -> datetime | list[datetime]:
    """Reject timestamps that cannot be compared safely."""
    candidates = values if isinstance(values, list) else [values]
    if any(value.tzinfo is None or value.utcoffset() is None for value in candidates):
        raise ValueError("timestamps must include a timezone")
    return values


def require_aware_timestamp(value: datetime) -> datetime:
    """Reject one timestamp that cannot be compared safely."""
    require_aware_timestamps(value)
    return value
