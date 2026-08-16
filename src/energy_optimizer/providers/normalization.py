"""Small provider-independent normalization utilities."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable, cast

ErrorFactory = Callable[[str], Exception]


def parse_aware_timestamp(
    value: Any,
    *,
    error_factory: ErrorFactory,
    missing_message: str,
    invalid_message: Callable[[Any], str],
    naive_message: str,
) -> datetime:
    """Parse an ISO timestamp and return it in UTC."""
    if not isinstance(value, str):
        raise error_factory(missing_message)
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise error_factory(invalid_message(value)) from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise error_factory(naive_message)
    return timestamp.astimezone(timezone.utc)


def as_utc(
    value: datetime,
    *,
    error_factory: ErrorFactory,
    message: str,
) -> datetime:
    """Require a timezone-aware datetime and return it in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise error_factory(message)
    return value.astimezone(timezone.utc)


def validate_finite_non_negative(
    value: object,
    *,
    error_factory: ErrorFactory,
    label: str,
) -> float:
    """Convert one numeric provider value and reject unsafe generation values."""
    if isinstance(value, bool):
        raise error_factory(f"{label} must be numeric")
    try:
        number = float(cast(str | float | int, value))
    except (TypeError, ValueError) as error:
        raise error_factory(f"{label} must be numeric") from error
    if not math.isfinite(number) or number < 0:
        raise error_factory(f"{label} must be finite and non-negative")
    return number
