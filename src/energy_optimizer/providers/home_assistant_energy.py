"""Pure Home Assistant energy-history normalization and aggregation.

Fetching and cleaning belong to ``home_assistant_history``. This module turns a
consumer's window of the shared cleaned history into hourly energy, applying the
consumer's own entity settings: counter deltas, reset, spike, jitter and recovery
handling, unit and state-class validation, physical limits, and signed
aggregation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter

from energy_optimizer.config import HomeAssistantEnergyEntityConfiguration
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistorySample,
    HomeAssistantError,
    HomeAssistantHistory,
)
from energy_optimizer.providers.interfaces import IntervalQuality
from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    as_utc,
    validate_hourly_period,
)
from energy_optimizer.providers.normalization import is_fresh as check_freshness

logger = logging.getLogger(__name__)

_KWH_PER_UNIT = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}
# A tolerance written as a decimal (0.001 kWh) must accept the same decrease
# computed from two decimal counter readings, which differs by float rounding.
_TOLERANCE_ROUNDING_KWH = 1e-9


@dataclass(frozen=True)
class HomeAssistantEnergySeries:
    """One normalized hourly energy contribution from Home Assistant."""

    start_time: datetime
    values_kw: tuple[float, ...]
    latest_observation_at: datetime
    quality: tuple[IntervalQuality, ...] = ()


@dataclass
class _CounterState:
    """Mutable state carried between cumulative-counter observations."""

    previous_value: float
    previous_reset: datetime | None
    reset_baseline: float | None = None
    previous_delta_kwh: float = 0.0
    previous_delta_hour: int | None = None
    previous_delta_timestamp: datetime | None = None
    previous_delta_start_value: float | None = None
    # Highest value reached before a tolerated decrease of a ``total`` counter.
    # While set, energy is counted only once the counter rises above it, so a
    # counter that climbs back after jitter is not counted twice.
    jitter_peak: float | None = None
    tolerated_decreases: list[tuple[datetime, float]] = field(default_factory=list)

    def record(
        self,
        timestamp: datetime,
        value: float,
        reset: datetime | None,
        accepted_delta_kwh: float,
        hour: int | None,
    ) -> None:
        """Record an observation and the delta assigned to its interval."""
        self.previous_delta_kwh = accepted_delta_kwh
        self.previous_delta_hour = hour if accepted_delta_kwh > 0 else None
        self.previous_delta_timestamp = timestamp if accepted_delta_kwh > 0 else None
        self.previous_delta_start_value = (
            self.previous_value if accepted_delta_kwh > 0 else None
        )
        self.previous_value = value
        self.previous_reset = reset


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
        allow_negative: bool = False,
    ) -> None:
        """Validate the requested period and the configured entities.

        ``allow_negative`` treats the combined signed expression as a net
        directional flow: a negative hourly value is clamped to zero for that
        hour instead of raising an error. This suits calculated
        battery-efficiency legs, where a signed expression nets one directional
        energy flow against another (for example battery charging energy minus
        directly consumed PV yield) and a negative net simply means none of
        that flow occurred, with the remainder belonging to a different leg or
        to export.

        Without ``allow_negative``, which household load and grid flow use to
        catch misconfigured add/subtract operations, a negative combined hourly
        value raises an error that names the hour by its UTC timestamp. The one
        exception is an hour that at least one contributing entity has already
        flagged suspect: a rejected or reset counter explains the negative
        value, so it is clamped to zero, the hour keeps its suspect quality,
        and one structured warning lists every clamped hour. Non-finite values
        are always rejected, whatever the quality flags.
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
        self.allow_negative = allow_negative
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
                "start_time=%s end_time=%s "
                "duration_ms=%.1f",
                self.label,
                entity_count,
                self.start_time.isoformat(),
                self.end_time.isoformat(),
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
        allow_negative = self.allow_negative
        contributions: list[tuple[HomeAssistantEnergySeries, str]] = [
            (self._normalize_entity(history, entity), entity.operation)
            for entity in self.entities
        ]

        aggregate_start = max(series.start_time for series, _ in contributions)
        if aggregate_start >= end:
            raise HomeAssistantError(
                f"Home Assistant {label} entities have no complete hourly history "
                "in the requested period"
            )
        value_count = int((end - aggregate_start).total_seconds() // 3600)
        values = [0.0] * value_count
        quality: list[IntervalQuality] = [IntervalQuality()] * value_count
        suspect_contributions: dict[int, list[IntervalQuality]] = {}
        for series, operation in contributions:
            offset = int((aggregate_start - series.start_time).total_seconds() // 3600)
            aligned = series.values_kw[offset : offset + value_count]
            if len(aligned) != value_count:
                raise HomeAssistantError(
                    f"Home Assistant {label} entities returned misaligned hourly series"
                )
            sign = 1.0 if operation == "add" else -1.0
            for index, value in enumerate(aligned):
                values[index] += sign * value
            aligned_quality = series.quality[offset : offset + value_count]
            if aligned_quality and len(aligned_quality) != value_count:
                raise HomeAssistantError(
                    f"Home Assistant {label} entities returned misaligned quality data"
                )
            for index, item in enumerate(aligned_quality):
                if item.status == "suspect":
                    quality[index] = item
                    suspect_contributions.setdefault(index, []).append(item)

        clamped_hours: list[tuple[datetime, list[IntervalQuality]]] = []
        for index, value in enumerate(values):
            hour_start = aggregate_start + timedelta(hours=index)
            if not math.isfinite(value):
                raise HomeAssistantError(
                    f"combined Home Assistant {label} data contains a "
                    f"non-finite value at {hour_start.isoformat()}; check add "
                    "and subtract operations"
                )
            if value < -1e-9 and not allow_negative:
                flagged = suspect_contributions.get(index)
                if not flagged:
                    raise HomeAssistantError(
                        f"combined Home Assistant {label} data contains a "
                        f"negative value at {hour_start.isoformat()}; check "
                        "add and subtract operations"
                    )
                clamped_hours.append((hour_start, flagged))
            values[index] = max(0.0, value)
        if clamped_hours:
            _log_clamped_hours(label, clamped_hours)

        return HomeAssistantEnergySeries(
            start_time=aggregate_start,
            values_kw=tuple(values),
            latest_observation_at=min(
                series.latest_observation_at for series, _ in contributions
            ),
            quality=(
                tuple(quality)
                if any(item.status == "suspect" for item in quality)
                else ()
            ),
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
            entity.decrease_tolerance_kwh,
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


def _log_clamped_hours(
    label: str,
    clamped_hours: list[tuple[datetime, list[IntervalQuality]]],
) -> None:
    """Emit one warning listing every negative hour clamped to zero."""
    described = ",".join(
        f"{hour_start.isoformat()}["
        + ";".join(f"{item.reason}:{item.entity_id}" for item in flagged)
        + "]"
        for hour_start, flagged in clamped_hours
    )
    logger.warning(
        "event=home_assistant_negative_hour_clamped component=home_assistant "
        "operation=aggregate label=%s clamped_hour_count=%s clamped_hours=%s",
        label,
        len(clamped_hours),
        described,
    )


def normalize_counter_history(
    entity: HomeAssistantEnergyEntityConfiguration,
    samples: tuple[HistorySample, ...],
    start_time: datetime,
    end_time: datetime,
) -> HomeAssistantEnergySeries:
    """Turn one entity's window of cleaned samples into hourly energy.

    The function is pure: its result depends only on its arguments, so normalizing
    a window of a shared series equals normalizing an independent fetch of it.
    """
    return _normalize_samples(
        _validate_samples(entity, samples), start_time, end_time, entity
    )


def _validate_samples(
    entity: HomeAssistantEnergyEntityConfiguration,
    samples: tuple[HistorySample, ...],
) -> list[tuple[datetime, float, datetime | None]]:
    """Apply the consumer's own state-class and unit settings to a window."""
    parsed: list[tuple[datetime, float, datetime | None]] = []
    for sample in samples:
        if sample.problem is not None:
            raise HomeAssistantError(sample.problem)
        state_class = (
            sample.state_class if sample.state_class is not None else entity.state_class
        )
        if state_class != entity.state_class:
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} reports state_class "
                f"{state_class!r}; expected {entity.state_class!r}"
            )
        if sample.unit in {"W", "kW"}:
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} reports "
                f"{sample.unit}, an instantaneous power unit; configure an "
                "energy entity reported in Wh, kWh, or MWh"
            )
        if sample.unit != entity.unit:
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} reports incompatible "
                f"unit {sample.unit!r}; expected {entity.unit!r}"
            )
        parsed.append((sample.timestamp, sample.value, sample.last_reset))
    if not parsed:
        raise HomeAssistantError(
            f"Home Assistant returned no history for {entity.entity_id} in the "
            "requested period"
        )
    return parsed


def _normalize_samples(
    parsed: list[tuple[datetime, float, datetime | None]],
    start_time: datetime,
    end_time: datetime,
    entity: HomeAssistantEnergyEntityConfiguration,
) -> HomeAssistantEnergySeries:
    start = _as_utc(start_time)
    end = _as_utc(end_time)
    factor = _KWH_PER_UNIT[entity.unit]
    timestamps = [timestamp for timestamp, _, _ in parsed]
    if len(timestamps) != len(set(timestamps)):
        raise HomeAssistantError(
            f"Home Assistant entity {entity.entity_id} contains duplicate timestamps"
        )
    baseline_index = (
        next(
            (
                index
                for index, (timestamp, _, _) in enumerate(parsed)
                if timestamp > start
            ),
            len(parsed),
        )
        - 1
    )
    effective_start = start
    if baseline_index < 0:
        baseline_index = 0
        effective_start = align_to_next_hour(parsed[baseline_index][0])
        logger.info(
            "event=provider_history_truncated component=home_assistant "
            "operation=normalize entity_id=%s requested_start=%s "
            "available_start=%s",
            entity.entity_id,
            start,
            effective_start,
        )
    baseline = parsed[baseline_index]
    hour_count = int((end - effective_start).total_seconds() // 3600)
    if hour_count <= 0:
        raise HomeAssistantError(
            f"Home Assistant entity {entity.entity_id} has no complete hourly "
            "history after its earliest usable observation"
        )
    values = [0.0] * hour_count
    quality = [IntervalQuality()] * hour_count
    state = _CounterState(baseline[1], baseline[2])
    latest_observation = baseline[0]
    for timestamp, value, reset in parsed[baseline_index + 1 :]:
        if timestamp > end:
            break
        delta = _counter_delta(
            state,
            timestamp,
            value,
            reset,
            effective_start,
            entity,
            values,
            quality,
        )
        delta_kwh = delta * factor
        maximum_delta_kwh = entity.maximum_interval_energy_kwh
        if delta_kwh > maximum_delta_kwh:
            _mark_quality(
                quality,
                timestamp,
                effective_start,
                entity.entity_id,
                "physical_limit_exceeded",
            )
            logger.warning(
                "event=home_assistant_delta_rejected "
                "component=home_assistant operation=normalize "
                "entity_id=%s timestamp=%s delta_kwh=%s "
                "maximum_interval_energy_kwh=%s reason=physical_limit_exceeded",
                entity.entity_id,
                timestamp.isoformat(),
                delta_kwh,
                maximum_delta_kwh,
            )
            delta = 0.0
        elapsed_seconds = (timestamp - effective_start).total_seconds()
        hour: int | None = None
        if elapsed_seconds > 0:
            hour = math.ceil(elapsed_seconds / 3600) - 1
            values[hour] += delta * factor
            accepted_delta_kwh = delta * factor
        else:
            accepted_delta_kwh = 0.0
        state.record(timestamp, value, reset, accepted_delta_kwh, hour)
        latest_observation = timestamp
    if state.tolerated_decreases:
        _log_tolerated_decreases(entity, state.tolerated_decreases)
    return HomeAssistantEnergySeries(
        start_time=effective_start,
        values_kw=tuple(values),
        latest_observation_at=latest_observation,
        quality=(
            tuple(quality) if any(item.status == "suspect" for item in quality) else ()
        ),
    )


def _counter_delta(
    state: _CounterState,
    timestamp: datetime,
    value: float,
    reset: datetime | None,
    effective_start: datetime,
    entity: HomeAssistantEnergyEntityConfiguration,
    values: list[float],
    quality: list[IntervalQuality],
) -> float:
    """Apply reset, spike, jitter, and recovery transitions to one observation.

    No transition raises: a decrease never fails the import. Home Assistant
    marks a genuine reset of a ``total`` counter by changing ``last_reset``.
    A decrease without that marker is measurement jitter when the counter
    stays within ``decrease_tolerance_kwh`` of the highest value it reached:
    the step contributes no energy and the climb back to that peak is not
    counted again. Measuring against the peak rather than the previous
    sample keeps a slow downward drift from being accepted step by step. A
    larger decrease, like any decrease of a ``total_increasing`` counter,
    is treated as a reset or a transient spike and marks the interval
    suspect.
    """
    previous_value = state.previous_value
    peak = previous_value if state.jitter_peak is None else state.jitter_peak
    decrease_kwh = (peak - value) * _KWH_PER_UNIT[entity.unit]
    tolerated_decrease = (
        entity.state_class == "total"
        and value < previous_value
        and decrease_kwh <= entity.decrease_tolerance_kwh + _TOLERANCE_ROUNDING_KWH
    )
    if reset != state.previous_reset:
        delta = 0.0
        state.jitter_peak = None
        state.reset_baseline = previous_value
        _mark_quality(
            quality,
            timestamp,
            effective_start,
            entity.entity_id,
            "counter_reset",
        )
        logger.warning(
            "event=home_assistant_counter_reset component=home_assistant "
            "operation=normalize entity_id=%s timestamp=%s "
            "previous_value=%s current_value=%s reason=reset_marker_changed",
            entity.entity_id,
            timestamp.isoformat(),
            previous_value,
            value,
        )
    elif tolerated_decrease:
        delta = 0.0
        state.jitter_peak = peak
        state.tolerated_decreases.append((timestamp, decrease_kwh))
    elif value < previous_value:
        delta = 0.0
        state.jitter_peak = None
        transient_spike = (
            state.previous_delta_kwh > 0
            and state.previous_delta_hour is not None
            and state.previous_delta_timestamp is not None
            and state.previous_delta_start_value is not None
            and math.isclose(
                value,
                state.previous_delta_start_value,
                rel_tol=0.01,
                abs_tol=0.001,
            )
        )
        if transient_spike:
            assert state.previous_delta_hour is not None
            assert state.previous_delta_timestamp is not None
            values[state.previous_delta_hour] -= state.previous_delta_kwh
            _mark_quality(
                quality,
                state.previous_delta_timestamp,
                effective_start,
                entity.entity_id,
                "transient_counter_spike",
            )
            _mark_quality(
                quality,
                timestamp,
                effective_start,
                entity.entity_id,
                "transient_counter_spike",
            )
            state.reset_baseline = None
            logger.warning(
                "event=home_assistant_counter_spike_corrected "
                "component=home_assistant operation=normalize entity_id=%s "
                "timestamp=%s previous_value=%s current_value=%s "
                "spike_start_value=%s retracted_delta_kwh=%s",
                entity.entity_id,
                timestamp.isoformat(),
                previous_value,
                value,
                state.previous_delta_start_value,
                state.previous_delta_kwh,
            )
        else:
            state.reset_baseline = previous_value
            _mark_quality(
                quality,
                timestamp,
                effective_start,
                entity.entity_id,
                "counter_reset",
            )
            tolerance_detail = (
                f" decrease_kwh={decrease_kwh:.6f} "
                f"decrease_tolerance_kwh={entity.decrease_tolerance_kwh}"
                if entity.state_class == "total"
                else ""
            )
            logger.warning(
                "event=home_assistant_counter_reset "
                "component=home_assistant operation=normalize entity_id=%s "
                "timestamp=%s previous_value=%s current_value=%s "
                "reason=counter_decreased%s",
                entity.entity_id,
                timestamp.isoformat(),
                previous_value,
                value,
                tolerance_detail,
            )
    elif value >= previous_value:
        if state.reset_baseline is not None and math.isclose(
            value, state.reset_baseline, rel_tol=0.01, abs_tol=0.001
        ):
            delta = 0.0
            state.jitter_peak = None
            _mark_quality(
                quality,
                timestamp,
                effective_start,
                entity.entity_id,
                "reset_recovery",
            )
            logger.warning(
                "event=home_assistant_counter_recovery "
                "component=home_assistant "
                "operation=normalize entity_id=%s timestamp=%s "
                "previous_value=%s current_value=%s reset_baseline=%s",
                entity.entity_id,
                timestamp.isoformat(),
                previous_value,
                value,
                state.reset_baseline,
            )
        elif state.jitter_peak is not None:
            # The counter climbs back after a tolerated decrease. Energy up
            # to the earlier peak was already counted.
            delta = max(0.0, value - state.jitter_peak)
            if value >= state.jitter_peak:
                state.jitter_peak = None
        else:
            delta = value - previous_value
        state.reset_baseline = None
    return delta


def _log_tolerated_decreases(
    entity: HomeAssistantEnergyEntityConfiguration,
    tolerated_decreases: list[tuple[datetime, float]],
) -> None:
    """Summarize the jitter ignored for one entity in a single log line."""
    logger.info(
        "event=home_assistant_counter_jitter_tolerated "
        "component=home_assistant operation=normalize entity_id=%s "
        "decrease_count=%s first_timestamp=%s largest_decrease_kwh=%.6f "
        "decrease_tolerance_kwh=%s",
        entity.entity_id,
        len(tolerated_decreases),
        tolerated_decreases[0][0].isoformat(),
        max(decrease for _, decrease in tolerated_decreases),
        entity.decrease_tolerance_kwh,
    )


def _mark_quality(
    quality: list[IntervalQuality],
    timestamp: datetime,
    start_time: datetime,
    entity_id: str,
    reason: str,
) -> None:
    """Mark the hourly interval containing a reset or recovery anomaly."""
    elapsed_seconds = (timestamp - start_time).total_seconds()
    if elapsed_seconds <= 0:
        return
    hour = math.ceil(elapsed_seconds / 3600) - 1
    if 0 <= hour < len(quality):
        quality[hour] = IntervalQuality(
            status="suspect", reason=reason, entity_id=entity_id
        )


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
