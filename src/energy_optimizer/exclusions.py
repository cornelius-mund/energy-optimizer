"""Excluded hours: the records that explain why an hour is not in imported history.

An hour of imported Home Assistant history is either fully valid or excluded.
An excluded hour has no value; it carries one or more causes instead. Every cause
names the entity, a reason code from the closed set below, a human-readable
message, and the exact data points that led to the exclusion.

This module is pure domain code: it knows nothing about Home Assistant, storage,
or HTTP.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, get_args

logger = logging.getLogger(__name__)
_HOUR_SECONDS = 3600

ExclusionReason = Literal[
    "unavailable",
    "non_numeric",
    "not_finite",
    "negative_value",
    "invalid_attribute",
    "counter_decrease",
    "step_after_decrease",
    "step_above_maximum",
    "hour_above_maximum",
    "last_reset_changed",
    "unit_mismatch",
    "unit_missing",
    "state_class_mismatch",
    "soc_out_of_range",
    "combined_negative",
    "combined_not_finite",
    "flagged_by_earlier_version",
    "history_unavailable",
]
"""Every reason an hour can be excluded. The set is closed and documented."""

EXCLUSION_REASONS: tuple[str, ...] = get_args(ExclusionReason)

MAX_DATA_POINTS_PER_ENTITY_HOUR = 50
"""Storage bound: data points kept per entity and excluded hour.

The number of offending data points is always kept in full in
``ExclusionCause.data_point_count``.
"""


@dataclass(frozen=True)
class ExcludedDataPoint:
    """One observation that contributed to an exclusion.

    ``state`` is the raw state exactly as Home Assistant reported it. For a
    counter step, ``previous_timestamp`` and ``previous_value`` describe the
    observation the step started from, ``value`` the one it ended at (both in
    the entity's own unit), ``step_kwh`` the energy of the step, and
    ``maximum_kwh`` the configured limit that applies. ``entity_id`` is set only
    when the data point belongs to another entity than its cause, as for the
    components of a combined value.
    """

    timestamp: datetime
    state: str | None = None
    unit: str | None = None
    entity_id: str | None = None
    previous_timestamp: datetime | None = None
    previous_value: float | None = None
    value: float | None = None
    step_kwh: float | None = None
    maximum_kwh: float | None = None


@dataclass(frozen=True)
class ExclusionCause:
    """One reason an hour is excluded, with the data points behind it."""

    reason: ExclusionReason
    message: str
    entity_id: str | None
    data_points: tuple[ExcludedDataPoint, ...]
    data_point_count: int

    @classmethod
    def of(
        cls,
        reason: ExclusionReason,
        message: str,
        entity_id: str | None,
        data_points: Sequence[ExcludedDataPoint],
    ) -> ExclusionCause:
        """Build a cause that counts every one of its data points."""
        return cls(reason, message, entity_id, tuple(data_points), len(data_points))


@dataclass(frozen=True)
class HourExclusion:
    """One excluded hour of one source and every cause that excludes it."""

    hour_start: datetime
    causes: tuple[ExclusionCause, ...]


def history_unavailable_exclusions(
    source: str, missing_start: datetime, missing_end: datetime
) -> tuple[HourExclusion, ...]:
    """Exclude every hour of a range that the provider no longer holds.

    The range is half-open: ``missing_start`` is the first missing hour and
    ``missing_end`` the first hour after it that has history again. Every hour
    gets the same cause, which names the whole range, so an operator reading one
    hour can see the extent of the gap. The cause belongs to no entity and holds
    no data points, because nothing was recorded for these hours. ``source`` names
    the history that has the gap in the warning that reports it.
    """
    hours = round((missing_end - missing_start).total_seconds() / _HOUR_SECONDS)
    message = (
        f"The provider holds no history from {missing_start.isoformat()} until "
        f"{missing_end.isoformat()} ({hours} hour{'' if hours == 1 else 's'}), so "
        f"{'this hour' if hours == 1 else 'these hours'} cannot be imported."
    )
    logger.warning(
        "event=provider_history_unavailable component=storage operation=merge "
        "source=%s hour_count=%s first_hour=%s last_hour=%s",
        source,
        hours,
        missing_start.isoformat(),
        (missing_end - timedelta(hours=1)).isoformat(),
    )
    cause = ExclusionCause.of("history_unavailable", message, None, [])
    return tuple(
        HourExclusion(missing_start + timedelta(hours=index), (cause,))
        for index in range(hours)
    )


def cap_data_points(causes: Iterable[ExclusionCause]) -> tuple[ExclusionCause, ...]:
    """Bound the stored data points per entity while keeping the full counts."""
    remaining: dict[str | None, int] = {}
    capped: list[ExclusionCause] = []
    for cause in causes:
        budget = remaining.get(cause.entity_id, MAX_DATA_POINTS_PER_ENTITY_HOUR)
        kept = cause.data_points[:budget]
        remaining[cause.entity_id] = budget - len(kept)
        capped.append(
            cause
            if len(kept) == len(cause.data_points)
            else replace(cause, data_points=kept)
        )
    return tuple(capped)


def merge_exclusions(*groups: Iterable[HourExclusion]) -> tuple[HourExclusion, ...]:
    """Union exclusions per hour, keeping each distinct cause once, sorted by hour."""
    by_hour: dict[datetime, list[ExclusionCause]] = {}
    for group in groups:
        for item in group:
            causes = by_hour.setdefault(item.hour_start, [])
            for cause in item.causes:
                if cause not in causes:
                    causes.append(cause)
    return tuple(
        HourExclusion(hour_start, tuple(causes))
        for hour_start, causes in sorted(by_hour.items())
    )


def exclusion_summary(
    exclusions: Iterable[HourExclusion],
) -> dict[ExclusionReason, int]:
    """Count excluded hours by reason; an hour counts once per distinct reason."""
    counts: dict[ExclusionReason, int] = {}
    for item in exclusions:
        for reason in dict.fromkeys(cause.reason for cause in item.causes):
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))
