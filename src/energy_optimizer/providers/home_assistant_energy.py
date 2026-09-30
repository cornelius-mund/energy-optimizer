"""Pure Home Assistant energy-history normalization and aggregation.

Fetching and cleaning belong to ``home_assistant_history``. This module turns a
consumer's window of the shared cleaned history into hourly energy, applying the
consumer's own entity settings.

The rule is deliberately simple: an hour is imported only if every data point
that contributes to it is valid. Nothing is repaired, tolerated, or estimated.
Every cause (an invalid sample, a counter decrease, a step above the maximum, ...)
has a time span, and every hour that overlaps that span is excluded and keeps the
cause with its exact data points. Excluded hours have no value.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
    cap_data_points,
    merge_exclusions,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistorySample,
    HomeAssistantError,
    HomeAssistantHistory,
)
from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    as_utc,
    validate_hourly_period,
)
from energy_optimizer.providers.normalization import is_fresh as check_freshness

logger = logging.getLogger(__name__)

_KWH_PER_UNIT = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
_HOUR = timedelta(hours=1)
# A combined value that is only a float rounding error below zero is zero.
_NEGATIVE_ROUNDING_KWH = 1e-9

_INVALID_SAMPLE_TEXT: dict[ExclusionReason, str] = {
    "unavailable": "reported an unknown or unavailable state",
    "non_numeric": "reported a non-numeric state",
    "not_finite": "reported a NaN or infinite state",
    "negative_value": "reported a negative counter value",
    "invalid_attribute": "reported an invalid state_class or last_reset attribute",
    "unit_missing": "reported no unit_of_measurement",
    "unit_mismatch": "reported a unit other than the configured one",
    "state_class_mismatch": "reported a state_class other than the configured one",
    "soc_out_of_range": "reported a state of charge outside 0 to 100 percent",
}


@dataclass(frozen=True)
class HomeAssistantEnergySeries:
    """One normalized hourly energy contribution from Home Assistant.

    An excluded hour has no value (``None``) and one entry in ``exclusions``.
    """

    start_time: datetime
    values_kw: tuple[float | None, ...]
    latest_observation_at: datetime
    exclusions: tuple[HourExclusion, ...] = ()


@dataclass(frozen=True, slots=True)
class _Observation:
    """A valid counter observation."""

    timestamp: datetime
    value: float
    last_reset: datetime | None
    recorded_at: datetime
    state: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class _Component:
    """One entity's hourly energy, aligned to the aggregate and signed."""

    sign: float
    entity_id: str
    series: HomeAssistantEnergySeries
    values: tuple[float | None, ...]


class EnergyAggregate:
    """One signed energy expression: what it needs and how it is built.

    ``needs`` declares the entities and time range, including the source's own
    lookback, so the orchestrator can import each entity once for all sources.
    ``build`` then normalizes every entity's window of the shared history with
    this expression's own entity settings and combines the contributions.
    """

    def __init__(
        self,
        entities: list[HomeAssistantEnergyEntityConfiguration] | None,
        start_time: datetime,
        end_time: datetime,
        history_lookback_seconds: float = 0,
        *,
        label: str,
    ) -> None:
        """Validate the requested period and the configured entities.

        A combined hour is excluded when any contributing entity is excluded for
        it, and also when the add and subtract operations make it negative or
        not finite; it is never clamped to zero.
        """
        self.start_time = _as_utc(start_time)
        self.end_time = _as_utc(end_time)
        _validate_period(
            self.start_time, self.end_time, history_lookback_seconds, label
        )
        if not entities:
            raise HomeAssistantError(
                f"no Home Assistant {label} energy entities are configured"
            )
        self.entities = tuple(entities)
        self.history_lookback_seconds = history_lookback_seconds
        self.label = label
        self._history_start = self.start_time - timedelta(
            seconds=history_lookback_seconds
        )

    def needs(self) -> tuple[HistoryNeed, ...]:
        """Declare the counter history of every entity for the imported range."""
        return tuple(
            HistoryNeed(entity.entity_id, "counter", self._history_start, self.end_time)
            for entity in self.entities
        )

    def build(self, history: HomeAssistantHistory) -> HomeAssistantEnergySeries:
        """Combine all entity contributions from the shared history."""
        started_at = perf_counter()
        entity_count = len(self.entities)
        try:
            result = self._build(history)
            logger.info(
                "event=home_assistant_history_aggregate component=home_assistant "
                "operation=aggregate status=success label=%s entity_count=%s "
                "start_time=%s end_time=%s hour_count=%s excluded_hour_count=%s "
                "duration_ms=%.1f",
                self.label,
                entity_count,
                self.start_time.isoformat(),
                self.end_time.isoformat(),
                len(result.values_kw),
                len(result.exclusions),
                (perf_counter() - started_at) * 1000,
            )
            return result
        except Exception as error:
            logger.warning(
                "event=home_assistant_history_aggregate component=home_assistant "
                "operation=aggregate status=failed label=%s entity_count=%s "
                "start_time=%s end_time=%s "
                "duration_ms=%.1f error_type=%s error=%s",
                self.label,
                entity_count,
                self.start_time.isoformat(),
                self.end_time.isoformat(),
                (perf_counter() - started_at) * 1000,
                error.__class__.__name__,
                error,
            )
            raise

    def _build(self, history: HomeAssistantHistory) -> HomeAssistantEnergySeries:
        """Align and combine entity contributions without summary logs."""
        end = self.end_time
        label = self.label
        series_list = [
            (self._normalize_entity(history, entity), entity)
            for entity in self.entities
        ]

        aggregate_start = max(series.start_time for series, _ in series_list)
        if aggregate_start >= end:
            raise HomeAssistantError(
                f"Home Assistant {label} entities have no complete hourly history "
                "in the requested period"
            )
        value_count = int((end - aggregate_start).total_seconds() // 3600)
        components: list[_Component] = []
        for series, entity in series_list:
            offset = int((aggregate_start - series.start_time).total_seconds() // 3600)
            values = series.values_kw[offset : offset + value_count]
            if len(values) != value_count:
                raise HomeAssistantError(
                    f"Home Assistant {label} entities returned misaligned hourly series"
                )
            sign = 1.0 if entity.operation == "add" else -1.0
            components.append(_Component(sign, entity.entity_id, series, values))

        entity_exclusions = merge_exclusions(
            *(
                [
                    item
                    for item in component.series.exclusions
                    if aggregate_start <= item.hour_start < end
                ]
                for component in components
            )
        )
        excluded = {item.hour_start for item in entity_exclusions}
        combined_exclusions: list[HourExclusion] = []
        values_out: list[float | None] = []
        for index in range(value_count):
            hour_start = aggregate_start + index * _HOUR
            if hour_start in excluded:
                values_out.append(None)
                continue
            total = sum(
                component.sign * _required(component.values[index])
                for component in components
            )
            if not math.isfinite(total):
                reason: ExclusionReason = "combined_not_finite"
                described = "not finite"
            elif total < -_NEGATIVE_ROUNDING_KWH:
                reason = "combined_negative"
                described = "negative"
            else:
                values_out.append(max(0.0, total))
                continue
            values_out.append(None)
            combined_exclusions.append(
                HourExclusion(
                    hour_start,
                    (
                        self._combined_cause(
                            reason, described, total, hour_start, components, index
                        ),
                    ),
                )
            )

        return HomeAssistantEnergySeries(
            start_time=aggregate_start,
            values_kw=tuple(values_out),
            latest_observation_at=min(
                component.series.latest_observation_at for component in components
            ),
            exclusions=merge_exclusions(entity_exclusions, combined_exclusions),
        )

    def _combined_cause(
        self,
        reason: ExclusionReason,
        described: str,
        total: float,
        hour_start: datetime,
        components: list[_Component],
        index: int,
    ) -> ExclusionCause:
        """Explain a combined hour by listing the signed energy of every entity."""
        points = [
            ExcludedDataPoint(
                timestamp=hour_start,
                entity_id=component.entity_id,
                step_kwh=component.sign * _required(component.values[index]),
            )
            for component in components
        ]
        return ExclusionCause.of(
            reason,
            f"The combined {self.label} energy is {described} "
            f"({_number(total)} kWh) in the hour starting "
            f"{hour_start.isoformat()}; check the add and subtract operations of "
            "the configured entities.",
            None,
            points,
        )

    def _normalize_entity(
        self,
        history: HomeAssistantHistory,
        entity: HomeAssistantEnergyEntityConfiguration,
    ) -> HomeAssistantEnergySeries:
        """Normalize one entity with this expression's own entity settings.

        Expressions that read an entity with identical settings and an identical
        window share one computation for the cycle.
        """
        key = (
            "energy-counter",
            entity.entity_id,
            entity.state_class,
            entity.unit,
            entity.maximum_interval_energy_kwh,
            self.start_time,
            self.end_time,
            self.history_lookback_seconds,
        )
        return history.computed(
            key,
            lambda: normalize_counter_history(
                entity,
                history.window(
                    entity.entity_id, "counter", self._history_start, self.end_time
                ),
                self.start_time,
                self.end_time,
            ),
        )


def overlapping_hours(
    axis_start: datetime, span_start: datetime, span_end: datetime, hour_count: int
) -> range:
    """Return the hour indexes from the hour of ``span_start`` to that of ``span_end``.

    Hour ``k`` covers ``(axis_start + k h, axis_start + (k + 1) h]``, so an instant
    exactly on a boundary belongs to the earlier hour, like every observation. An
    invalid state that begins exactly at an hour's closing boundary therefore
    excludes that hour: its closing reading is missing. The result is clipped to
    the axis and is empty when the span lies entirely outside it.
    """
    first = max(0, hour_index(axis_start, span_start))
    last = min(hour_count - 1, hour_index(axis_start, span_end))
    return range(first, last + 1)


def hour_index(axis_start: datetime, timestamp: datetime) -> int:
    """Return the index of the hour an observation at ``timestamp`` belongs to."""
    return -((axis_start - timestamp) // _HOUR) - 1


def normalize_counter_history(
    entity: HomeAssistantEnergyEntityConfiguration,
    samples: tuple[HistorySample, ...],
    start_time: datetime,
    end_time: datetime,
) -> HomeAssistantEnergySeries:
    """Turn one entity's window of cleaned samples into hourly energy.

    The function is pure: its result depends only on its arguments, so normalizing
    a window of a shared series equals normalizing an independent fetch of it.

    Consecutive valid observations form steps, and the step's energy belongs to
    the hour of its later observation. Home Assistant records only state
    changes, so the counter kept its value between two observations. That is why
    a step affects only the hours of its own observations, while an invalid
    state, which stays in force until the next sample, affects every hour it is
    in force:

    - invalid samples exclude every hour from the first invalid sample to the
      first valid observation after them, so the hour in which the entity
      returns is excluded too;
    - a decrease, the step directly after a decrease (a reset cannot be told
      apart from a glitch), and a changed ``last_reset`` exclude the hours of
      both observations of the step, which also catches a spike that was
      recorded just before it fell back;
    - a step above the maximum excludes the hour of its later observation;
    - an hour whose total exceeds the maximum is excluded.
    """
    start = _as_utc(start_time)
    end = _as_utc(end_time)
    _validate_samples(entity, samples)
    factor = _KWH_PER_UNIT[entity.unit]
    maximum_kwh = entity.maximum_interval_energy_kwh

    baseline_index = _last_index_at_or_before(samples, start)
    effective_start = start
    if baseline_index is None:
        effective_start = align_to_next_hour(samples[0].timestamp)
        baseline_index = _last_index_at_or_before(samples, effective_start) or 0
        logger.info(
            "event=provider_history_truncated component=home_assistant "
            "operation=normalize entity_id=%s requested_start=%s "
            "available_start=%s",
            entity.entity_id,
            start,
            effective_start,
        )
    hour_count = int((end - effective_start).total_seconds() // 3600)
    if hour_count <= 0:
        raise HomeAssistantError(
            f"Home Assistant entity {entity.entity_id} has no complete hourly "
            "history after its earliest usable observation"
        )

    energy = [0.0] * hour_count
    causes_by_hour: dict[int, list[ExclusionCause]] = {}

    def exclude_span(
        span_start: datetime, span_end: datetime, causes: list[ExclusionCause]
    ) -> None:
        for hour in overlapping_hours(
            effective_start, span_start, span_end, hour_count
        ):
            causes_by_hour.setdefault(hour, []).extend(causes)

    def exclude_step(
        earlier: _Observation,
        later: _Observation,
        causes: list[ExclusionCause],
    ) -> None:
        """Exclude the hours of a step's observations, each cause where it applies."""
        for cause in causes:
            hours = {hour_index(effective_start, later.timestamp)}
            if cause.reason != "step_above_maximum":
                hours.add(hour_index(effective_start, earlier.timestamp))
            for hour in hours:
                if 0 <= hour < hour_count:
                    causes_by_hour.setdefault(hour, []).append(cause)

    last: _Observation | None = None
    gap: list[tuple[ExclusionReason, ExcludedDataPoint]] = []
    gap_start = effective_start
    after_decrease = False
    latest_observation = samples[baseline_index].timestamp
    for sample in samples[baseline_index:]:
        if sample.timestamp > end:
            break
        reason = _sample_reason(entity, sample)
        if reason is not None:
            if not gap:
                gap_start = sample.timestamp
            gap.append(
                (
                    reason,
                    ExcludedDataPoint(sample.recorded_at, sample.state, sample.unit),
                )
            )
            continue
        assert sample.value is not None
        observation = _Observation(
            sample.timestamp,
            sample.value,
            sample.last_reset,
            sample.recorded_at,
            sample.state,
            sample.unit,
        )
        if gap:
            exclude_span(
                gap_start,
                observation.timestamp,
                gap_causes(
                    entity.entity_id,
                    gap,
                    last.recorded_at if last else None,
                    observation.recorded_at,
                ),
            )
            gap = []
            after_decrease = False
        elif last is not None:
            step_kwh = (observation.value - last.value) * factor
            causes = _step_causes(
                entity, last, observation, step_kwh, maximum_kwh, after_decrease
            )
            after_decrease = step_kwh < 0
            if causes:
                exclude_step(last, observation, causes)
            elif observation.timestamp > effective_start:
                energy[hour_index(effective_start, observation.timestamp)] += step_kwh
        last = observation
        latest_observation = observation.timestamp
    if gap:
        exclude_span(
            gap_start,
            end,
            gap_causes(entity.entity_id, gap, last.recorded_at if last else None, None),
        )

    for hour, total in enumerate(energy):
        if total > maximum_kwh:
            hour_start = effective_start + hour * _HOUR
            causes_by_hour.setdefault(hour, []).append(
                ExclusionCause.of(
                    "hour_above_maximum",
                    f"{entity.entity_id} accumulated {_number(total)} kWh in the "
                    f"hour starting {hour_start.isoformat()}, above the maximum of "
                    f"{_number(maximum_kwh)} kWh.",
                    entity.entity_id,
                    [
                        ExcludedDataPoint(
                            hour_start, step_kwh=total, maximum_kwh=maximum_kwh
                        )
                    ],
                )
            )

    return HomeAssistantEnergySeries(
        start_time=effective_start,
        values_kw=tuple(
            None if hour in causes_by_hour else energy[hour]
            for hour in range(hour_count)
        ),
        latest_observation_at=latest_observation,
        exclusions=tuple(
            HourExclusion(effective_start + hour * _HOUR, cap_data_points(causes))
            for hour, causes in sorted(causes_by_hour.items())
        ),
    )


def _validate_samples(
    entity: HomeAssistantEnergyEntityConfiguration,
    samples: tuple[HistorySample, ...],
) -> None:
    """Reject only what makes the entity unusable as a whole, not single samples."""
    if not samples:
        raise HomeAssistantError(
            f"Home Assistant returned no history for {entity.entity_id} in the "
            "requested period"
        )
    for sample in samples:
        if sample.unit in {"W", "kW"}:
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} reports "
                f"{sample.unit}, an instantaneous power unit; configure an "
                "energy entity reported in Wh, kWh, or MWh"
            )
    timestamps = [sample.timestamp for sample in samples]
    if len(timestamps) != len(set(timestamps)):
        raise HomeAssistantError(
            f"Home Assistant entity {entity.entity_id} contains duplicate timestamps"
        )


def _last_index_at_or_before(
    samples: tuple[HistorySample, ...], timestamp: datetime
) -> int | None:
    """Return the index of the last sample recorded at or before ``timestamp``."""
    index = bisect_right(samples, timestamp, key=_sample_timestamp) - 1
    return index if index >= 0 else None


def _sample_timestamp(sample: HistorySample) -> datetime:
    return sample.timestamp


def _required(value: float | None) -> float:
    """Return the value of an hour that no entity excluded."""
    if value is None:  # pragma: no cover - guarded by the exclusion check
        raise HomeAssistantError("an included hour has no value")
    return value


def _sample_reason(
    entity: HomeAssistantEnergyEntityConfiguration, sample: HistorySample
) -> ExclusionReason | None:
    """Return why a sample cannot be used for this entity, or ``None``."""
    if sample.invalid is not None:
        return sample.invalid
    state_class = (
        sample.state_class if sample.state_class is not None else entity.state_class
    )
    if state_class != entity.state_class:
        return "state_class_mismatch"
    if sample.unit is None:
        return "unit_missing"
    if sample.unit != entity.unit:
        return "unit_mismatch"
    return None


def _step_causes(
    entity: HomeAssistantEnergyEntityConfiguration,
    earlier: _Observation,
    later: _Observation,
    step_kwh: float,
    maximum_kwh: float,
    after_decrease: bool,
) -> list[ExclusionCause]:
    """Return every cause that makes one counter step untrusted."""
    point = ExcludedDataPoint(
        later.recorded_at,
        later.state,
        later.unit,
        previous_timestamp=earlier.recorded_at,
        previous_value=earlier.value,
        value=later.value,
        step_kwh=step_kwh,
        maximum_kwh=maximum_kwh,
    )
    entity_id = entity.entity_id
    unit = entity.unit
    between = (
        f"{_number(earlier.value)} {unit} at {earlier.recorded_at.isoformat()} to "
        f"{_number(later.value)} {unit} at {later.recorded_at.isoformat()}"
    )
    causes: list[ExclusionCause] = []
    if later.last_reset != earlier.last_reset:
        causes.append(
            ExclusionCause.of(
                "last_reset_changed",
                f"The last_reset marker of {entity_id} changed between "
                f"{earlier.recorded_at.isoformat()} and "
                f"{later.recorded_at.isoformat()}.",
                entity_id,
                [point],
            )
        )
    if step_kwh < 0:
        causes.append(
            ExclusionCause.of(
                "counter_decrease",
                f"{entity_id} decreased from {between}.",
                entity_id,
                [point],
            )
        )
    else:
        if after_decrease:
            causes.append(
                ExclusionCause.of(
                    "step_after_decrease",
                    f"{entity_id} changed from {between}, directly after a "
                    "decrease; a reset cannot be told apart from a glitch, so the "
                    "step is not trusted.",
                    entity_id,
                    [point],
                )
            )
        if step_kwh > maximum_kwh:
            causes.append(
                ExclusionCause.of(
                    "step_above_maximum",
                    f"{entity_id} rose by {_number(step_kwh)} kWh from {between}, "
                    f"above the maximum of {_number(maximum_kwh)} kWh.",
                    entity_id,
                    [point],
                )
            )
    return causes


def gap_causes(
    entity_id: str,
    gap: list[tuple[ExclusionReason, ExcludedDataPoint]],
    valid_before_at: datetime | None,
    valid_after_at: datetime | None,
) -> list[ExclusionCause]:
    """Group the invalid samples between two valid observations by reason.

    ``valid_before_at`` and ``valid_after_at`` are the times Home Assistant
    recorded the valid observations around the gap; ``None`` means there is none.
    """
    by_reason: dict[ExclusionReason, list[ExcludedDataPoint]] = {}
    for reason, point in gap:
        by_reason.setdefault(reason, []).append(point)
    surrounding = (
        "last valid value "
        + (f"at {valid_before_at.isoformat()}" if valid_before_at else "none before")
        + ", next valid value "
        + (
            f"at {valid_after_at.isoformat()}"
            if valid_after_at
            else "none in the imported period"
        )
    )
    return [
        ExclusionCause.of(
            reason,
            f"{entity_id} {_INVALID_SAMPLE_TEXT[reason]} in {len(points)} "
            f"sample{'s' if len(points) != 1 else ''}, first at "
            f"{points[0].timestamp.isoformat()} ({surrounding}).",
            entity_id,
            points,
        )
        for reason, points in by_reason.items()
    ]


def _number(value: float) -> str:
    """Format a number for a message without float noise or lost digits."""
    return f"{value:.10g}"


def _as_utc(value: datetime) -> datetime:
    return as_utc(
        value,
        error_factory=HomeAssistantError,
        message="Home Assistant import times must include a timezone",
    )


def latest_completed_hour(value: datetime) -> datetime:
    """Return the UTC boundary of the latest completed hourly interval."""
    return value.replace(minute=0, second=0, microsecond=0)


def _validate_period(
    start_time: datetime,
    end_time: datetime,
    history_lookback_seconds: float,
    label: str,
) -> None:
    validate_hourly_period(
        start_time,
        end_time,
        error_factory=HomeAssistantError,
        start_message=f"{label} start_time must be aligned to the hour",
        end_message=f"{label} end_time must be aligned to the hour",
        order_message=f"{label} end_time must be after start_time",
        whole_hours_message=(
            f"{label} requested period must contain whole hourly intervals"
        ),
        check_end_alignment=False,
    )
    if not math.isfinite(history_lookback_seconds) or history_lookback_seconds < 0:
        raise HomeAssistantError(
            "history_lookback_seconds must be finite and non-negative"
        )


def is_fresh(
    latest_observation_at: datetime,
    max_data_age_seconds: float | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Check whether an observation is within the configured age threshold."""
    return check_freshness(
        latest_observation_at,
        max_data_age_seconds,
        now=now,
        error_factory=HomeAssistantError,
        now_message="Home Assistant freshness times must include a timezone",
        observed_message="Home Assistant observations must include a timezone",
    )
