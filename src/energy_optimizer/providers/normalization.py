"""Small provider-independent normalization utilities."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
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


def is_fresh(
    observed_at: datetime,
    max_data_age_seconds: float | None,
    *,
    expires_at: datetime | None = None,
    now: datetime | None = None,
    error_factory: ErrorFactory,
    now_message: str,
    observed_message: str | None = None,
) -> bool:
    """Check an observation's optional age and expiry constraints."""
    if max_data_age_seconds is None and expires_at is None:
        return True
    current = as_utc(
        now or datetime.now(timezone.utc),
        error_factory=error_factory,
        message=now_message,
    )
    observed = (
        as_utc(
            observed_at,
            error_factory=error_factory,
            message=observed_message or now_message,
        )
        if max_data_age_seconds is not None
        else observed_at
    )
    if expires_at is not None and current >= expires_at:
        return False
    return (
        max_data_age_seconds is None
        or (current - observed).total_seconds() < max_data_age_seconds
    )


def validate_hourly_period(
    start: datetime,
    end: datetime | None,
    *,
    error_factory: ErrorFactory,
    start_message: str,
    end_message: str,
    order_message: str,
    check_order_first: bool = True,
    whole_hours_message: str | None = None,
    check_end_alignment: bool = True,
) -> None:
    """Validate an optional half-open period whose boundaries are hourly."""
    if not check_order_first and _has_subhour_component(start):
        raise error_factory(start_message)
    if end is None:
        return
    if end <= start:
        raise error_factory(order_message)
    if whole_hours_message is not None and (end - start).total_seconds() % 3600 != 0:
        raise error_factory(whole_hours_message)
    if check_order_first and _has_subhour_component(start):
        raise error_factory(start_message)
    if check_end_alignment and _has_subhour_component(end):
        raise error_factory(end_message)


def align_to_next_hour(value: datetime) -> datetime:
    """Return the current hour when aligned, otherwise the following hour."""
    aligned = value.replace(minute=0, second=0, microsecond=0)
    return aligned if value == aligned else aligned + timedelta(hours=1)


def _has_subhour_component(value: datetime) -> bool:
    return bool(value.minute or value.second or value.microsecond)


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
