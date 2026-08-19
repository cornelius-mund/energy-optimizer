"""Shared Home Assistant energy-history retrieval and normalization."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
    HomeAssistantConfiguration,
    HomeAssistantEnergyEntityConfiguration,
)
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import IntervalQuality
from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    as_utc,
    parse_aware_timestamp,
    validate_hourly_period,
)
from energy_optimizer.providers.normalization import is_fresh as check_freshness

logger = logging.getLogger(__name__)

HOME_ASSISTANT_HISTORY_CHUNK = timedelta(days=7)


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant energy data cannot be imported safely."""


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


class HomeAssistantEnergyAggregator:
    """Retrieve and combine signed cumulative-energy entity contributions."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._http = JsonHttpClient(client)

    def aggregate(
        self,
        entities: list[HomeAssistantEnergyEntityConfiguration] | None,
        start_time: datetime,
        end_time: datetime,
        history_lookback_seconds: float = 0,
        *,
        label: str,
        allow_negative: bool = False,
    ) -> HomeAssistantEnergySeries:
        """Fetch, align, and combine all configured entity contributions.

        ``allow_negative`` treats the combined signed expression as a net
        directional flow: a negative hourly value is clamped to zero for that
        hour instead of raising an error. This suits calculated
        battery-efficiency legs, where a signed expression nets one directional
        energy flow against another (for example battery charging energy minus
        directly consumed PV yield) and a negative net simply means none of
        that flow occurred, with the remainder belonging to a different leg or
        to export. Non-finite values are always rejected. Strict rejection of
        any negative combined value remains the default, which household load
        and grid flow rely on to catch misconfigured add/subtract operations.
        """
        started_at = perf_counter()
        entity_count = len(entities or [])
        try:
            result = self._aggregate(
                entities,
                start_time,
                end_time,
                history_lookback_seconds,
                label=label,
                allow_negative=allow_negative,
            )
            logger.info(
                "event=home_assistant_history_aggregate component=home_assistant "
                "operation=aggregate status=success label=%s entity_count=%s "
                "start_time=%s end_time=%s "
                "duration_ms=%.1f",
                label,
                entity_count,
                start_time.isoformat(),
                end_time.isoformat(),
                (perf_counter() - started_at) * 1000,
            )
            return result
        except Exception as error:
            logger.warning(
                "event=home_assistant_history_aggregate component=home_assistant "
                "operation=aggregate status=failed label=%s entity_count=%s "
                "start_time=%s end_time=%s "
                "duration_ms=%.1f error_type=%s error=%s",
                label,
                entity_count,
                start_time.isoformat(),
                end_time.isoformat(),
                (perf_counter() - started_at) * 1000,
                error.__class__.__name__,
                error,
            )
            raise

    def _aggregate(
        self,
        entities: list[HomeAssistantEnergyEntityConfiguration] | None,
        start_time: datetime,
        end_time: datetime,
        history_lookback_seconds: float,
        *,
        label: str,
        allow_negative: bool = False,
    ) -> HomeAssistantEnergySeries:
        """Fetch, align, and combine entity contributions without summary logs."""
        start = self._as_utc(start_time)
        end = self._as_utc(end_time)
        self._validate_period(start, end, history_lookback_seconds, label)
        contributions: list[tuple[HomeAssistantEnergySeries, str]] = []
        for entity in entities or []:
            contributions.append(
                (
                    self._fetch_entity(
                        entity,
                        start,
                        end,
                        history_lookback_seconds,
                        label,
                    ),
                    entity.operation,
                )
            )

        if not contributions:
            raise HomeAssistantError(
                f"no Home Assistant {label} energy entities are configured"
            )

        aggregate_start = max(series.start_time for series, _ in contributions)
        if aggregate_start >= end:
            raise HomeAssistantError(
                f"Home Assistant {label} entities have no complete hourly history "
                "in the requested period"
            )
        value_count = int((end - aggregate_start).total_seconds() // 3600)
        values = [0.0] * value_count
        quality: list[IntervalQuality] = [IntervalQuality()] * value_count
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

        for index, value in enumerate(values):
            if not math.isfinite(value):
                raise HomeAssistantError(
                    f"combined Home Assistant {label} data contains a "
                    f"non-finite value at hour {index}; check add and subtract "
                    "operations"
                )
            if value < -1e-9 and not allow_negative:
                raise HomeAssistantError(
                    f"combined Home Assistant {label} data contains a negative "
                    f"value at hour {index}; check add and subtract operations"
                )
            values[index] = max(0.0, value)

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

    def _fetch_entity(
        self,
        entity: HomeAssistantEnergyEntityConfiguration,
        start_time: datetime,
        end_time: datetime,
        history_lookback_seconds: float,
        label: str,
    ) -> HomeAssistantEnergySeries:
        chunk_start = start_time - timedelta(seconds=history_lookback_seconds)
        records: list[dict[str, Any]] = []
        record_indexes: dict[datetime, int] = {}
        while chunk_start < end_time:
            chunk_end = min(
                chunk_start + HOME_ASSISTANT_HISTORY_CHUNK,
                end_time,
            )
            response = self._request(
                self._history_url(chunk_start, chunk_end, entity.entity_id),
                entity,
                chunk_start,
                chunk_end,
                label,
            )
            chunk_records = self._parse_history(
                response, entity, label, allow_empty=True
            )
            chunk_timestamps: set[datetime] = set()
            for record in chunk_records:
                timestamp = self._parse_timestamp(
                    record.get("last_updated", record.get("last_changed")),
                    label,
                )
                if timestamp in chunk_timestamps:
                    # Preserve duplicate records within one provider response so
                    # normalization can reject the malformed history explicitly.
                    records.append(record)
                    continue
                chunk_timestamps.add(timestamp)
                existing_index = record_indexes.get(timestamp)
                if existing_index is None:
                    record_indexes[timestamp] = len(records)
                    records.append(record)
                else:
                    # Home Assistant may include a boundary observation in both
                    # adjacent half-open responses; retain the later response once.
                    records[existing_index] = record
            chunk_start = chunk_end
        if not records:
            raise HomeAssistantError(
                f"Home Assistant returned no {label} history for {entity.entity_id}"
            )
        return self._normalize_records(records, start_time, end_time, entity, label)

    def _history_url(
        self, start_time: datetime, end_time: datetime, entity_id: str
    ) -> str:
        base_url = str(self.configuration.base_url).rstrip("/")
        encoded_start = quote(self._as_utc(start_time).isoformat(), safe="")
        return (
            f"{base_url}/api/history/period/{encoded_start}"
            f"?end_time={quote(self._as_utc(end_time).isoformat(), safe='')}"
            f"&filter_entity_id={quote(entity_id)}"
        )

    def _request(
        self,
        url: str,
        entity: HomeAssistantEnergyEntityConfiguration,
        start_time: datetime,
        end_time: datetime,
        label: str,
    ) -> Any:
        return self._http.get_home_assistant_json(
            url,
            token=self.configuration.token.get_secret_value(),
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            not_found_message=(
                f"Home Assistant {label} history was not found for "
                f"{entity.entity_id}; check the configured entity ID and endpoint"
            ),
            status_message=lambda status: (
                f"Home Assistant returned HTTP {status} while retrieving "
                f"{label} history"
            ),
            timeout_message=(
                "Home Assistant request timed out; check the endpoint and timeout"
            ),
            transport_message="Home Assistant request failed: transport error",
            malformed_message=(
                f"Home Assistant returned malformed JSON for {label} history"
            ),
            log_event="home_assistant_history_request",
            component="home_assistant",
            operation="history_request",
            success_log_level=logging.DEBUG,
            error_log_level=logging.DEBUG,
            log_context=(
                f"entity_id={entity.entity_id} start_time={start_time.isoformat()} "
                f"end_time={end_time.isoformat()}"
            ),
        )

    @staticmethod
    def _parse_history(
        payload: Any,
        entity: HomeAssistantEnergyEntityConfiguration,
        label: str,
        *,
        allow_empty: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise HomeAssistantError(
                f"Home Assistant history for {entity.entity_id} must contain one "
                "entity series"
            )
        if not payload or (len(payload) == 1 and not payload[0]):
            if allow_empty:
                return []
            raise HomeAssistantError(
                f"Home Assistant returned no {label} history for {entity.entity_id}"
            )
        if len(payload) != 1:
            raise HomeAssistantError(
                f"Home Assistant history for {entity.entity_id} must contain one "
                "entity series"
            )
        series = payload[0]
        if not isinstance(series, list):
            raise HomeAssistantError(
                f"Home Assistant history for {entity.entity_id} must contain a list "
                "of records"
            )
        if not series and allow_empty:
            return []
        if not all(isinstance(record, dict) for record in series):
            raise HomeAssistantError(
                f"Home Assistant history for {entity.entity_id} contains an invalid "
                "record"
            )
        return series

    def _normalize_records(
        self,
        records: list[dict[str, Any]],
        start_time: datetime,
        end_time: datetime,
        entity: HomeAssistantEnergyEntityConfiguration,
        label: str,
    ) -> HomeAssistantEnergySeries:
        parsed, skipped_records = self._parse_records(records, entity, label)
        return self._normalize_parsed_records(
            parsed, skipped_records, start_time, end_time, entity
        )

    def _parse_records(
        self,
        records: list[dict[str, Any]],
        entity: HomeAssistantEnergyEntityConfiguration,
        label: str,
    ) -> tuple[list[tuple[datetime, float, datetime | None]], list[datetime]]:
        raw_records: list[tuple[datetime, dict[str, Any]]] = []
        skipped_records: list[datetime] = []
        for record in records:
            timestamp = self._parse_timestamp(
                record.get("last_updated", record.get("last_changed")), label
            )
            state = record.get("state")
            if isinstance(state, str) and state in {"unknown", "unavailable"}:
                skipped_records.append(timestamp)
                continue
            raw_records.append((timestamp, record))

        if skipped_records:
            logger.warning(
                "Home Assistant entity %s has %d unknown or unavailable history "
                "samples between %s and %s; skipped them without assigning energy",
                entity.entity_id,
                len(skipped_records),
                min(skipped_records).isoformat(),
                max(skipped_records).isoformat(),
            )

        raw_records.sort(key=lambda record: record[0])
        parsed: list[tuple[datetime, float, datetime | None]] = []
        previous_unit: str | None = None
        previous_reset: datetime | None = None
        previous_state_class: str | None = None
        for timestamp, record in raw_records:
            state = record.get("state")
            if not isinstance(state, str) or state in {"unknown", "unavailable"}:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} contains an "
                    f"unavailable value at {timestamp.isoformat()}"
                )
            try:
                value = float(state)
            except (TypeError, ValueError) as error:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} contains a "
                    f"non-numeric value at {timestamp.isoformat()}"
                ) from error
            if not math.isfinite(value) or value < 0:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} contains an invalid "
                    f"value at {timestamp.isoformat()}"
                )
            attributes = record.get("attributes")
            if isinstance(attributes, dict) and isinstance(
                attributes.get("unit_of_measurement"), str
            ):
                normalized_unit = attributes["unit_of_measurement"].strip()
            elif previous_unit is not None:
                normalized_unit = previous_unit
            else:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} is missing "
                    "unit_of_measurement"
                )
            if isinstance(attributes, dict) and "state_class" in attributes:
                normalized_state_class = attributes["state_class"]
                if not isinstance(normalized_state_class, str):
                    raise HomeAssistantError(
                        f"Home Assistant entity {entity.entity_id} has an invalid "
                        "state_class"
                    )
            elif previous_state_class is not None:
                normalized_state_class = previous_state_class
            else:
                normalized_state_class = entity.state_class
            if normalized_state_class != entity.state_class:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} reports state_class "
                    f"{normalized_state_class!r}; expected {entity.state_class!r}"
                )
            if isinstance(attributes, dict) and "last_reset" in attributes:
                raw_reset = attributes["last_reset"]
                if raw_reset is None:
                    normalized_reset = None
                elif isinstance(raw_reset, str):
                    normalized_reset = self._parse_timestamp(raw_reset, label)
                else:
                    raise HomeAssistantError(
                        f"Home Assistant entity {entity.entity_id} has an invalid "
                        "last_reset timestamp"
                    )
            else:
                normalized_reset = previous_reset
            if normalized_unit in {"W", "kW"}:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} reports "
                    f"{normalized_unit}, an instantaneous power unit; configure an "
                    "energy entity reported in Wh, kWh, or MWh"
                )
            if normalized_unit != entity.unit:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} reports incompatible "
                    f"unit {normalized_unit!r}; expected {entity.unit!r}"
                )
            parsed.append((timestamp, value, normalized_reset))
            previous_unit = normalized_unit
            previous_reset = normalized_reset
            previous_state_class = normalized_state_class

        if not parsed:
            if skipped_records:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} has no usable "
                    "history after skipping unknown or unavailable records"
                )
            raise HomeAssistantError(
                f"Home Assistant returned no usable history for {entity.entity_id}"
            )
        return parsed, skipped_records

    def _normalize_parsed_records(
        self,
        parsed: list[tuple[datetime, float, datetime | None]],
        skipped_records: list[datetime],
        start_time: datetime,
        end_time: datetime,
        entity: HomeAssistantEnergyEntityConfiguration,
    ) -> HomeAssistantEnergySeries:
        start = self._as_utc(start_time)
        end = self._as_utc(end_time)
        factor = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}[entity.unit]
        timestamps = [timestamp for timestamp, _, _ in parsed]
        if len(timestamps) != len(set(timestamps)):
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} contains duplicate "
                "timestamps"
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
            effective_start = self._first_complete_hour(parsed[baseline_index][0])
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
            delta = self._counter_delta(
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
                self._mark_quality(
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
        return HomeAssistantEnergySeries(
            start_time=effective_start,
            values_kw=tuple(values),
            latest_observation_at=latest_observation,
            quality=(
                tuple(quality)
                if any(item.status == "suspect" for item in quality)
                else ()
            ),
        )

    def _counter_delta(
        self,
        state: _CounterState,
        timestamp: datetime,
        value: float,
        reset: datetime | None,
        effective_start: datetime,
        entity: HomeAssistantEnergyEntityConfiguration,
        values: list[float],
        quality: list[IntervalQuality],
    ) -> float:
        """Apply reset, spike, and recovery transitions to one observation."""
        previous_value = state.previous_value
        if reset != state.previous_reset:
            delta = 0.0
            state.reset_baseline = previous_value
            self._mark_quality(
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
        elif value < previous_value and entity.state_class == "total_increasing":
            delta = 0.0
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
                self._mark_quality(
                    quality,
                    state.previous_delta_timestamp,
                    effective_start,
                    entity.entity_id,
                    "transient_counter_spike",
                )
                self._mark_quality(
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
                self._mark_quality(
                    quality,
                    timestamp,
                    effective_start,
                    entity.entity_id,
                    "counter_reset",
                )
                logger.warning(
                    "event=home_assistant_counter_reset "
                    "component=home_assistant operation=normalize entity_id=%s "
                    "timestamp=%s previous_value=%s current_value=%s "
                    "reason=counter_decreased",
                    entity.entity_id,
                    timestamp.isoformat(),
                    previous_value,
                    value,
                )
        elif value >= previous_value:
            if state.reset_baseline is not None and math.isclose(
                value, state.reset_baseline, rel_tol=0.01, abs_tol=0.001
            ):
                delta = 0.0
                self._mark_quality(
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
            else:
                delta = value - previous_value
            state.reset_baseline = None
        elif entity.state_class == "total":
            raise HomeAssistantError(
                f"Home Assistant total entity {entity.entity_id} decreased "
                "without a changed last_reset timestamp"
            )
        return delta

    @staticmethod
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

    @staticmethod
    def _parse_timestamp(value: Any, label: str) -> datetime:
        return parse_aware_timestamp(
            value,
            error_factory=HomeAssistantError,
            missing_message=(f"Home Assistant {label} history has a missing timestamp"),
            invalid_message=lambda raw: (
                f"Home Assistant returned an invalid timestamp: {raw!r}"
            ),
            naive_message=(
                f"Home Assistant {label} timestamps must include a timezone"
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return as_utc(
            value,
            error_factory=HomeAssistantError,
            message="Home Assistant import times must include a timezone",
        )

    @staticmethod
    def _latest_completed_hour(value: datetime) -> datetime:
        return value.replace(minute=0, second=0, microsecond=0)

    @classmethod
    def _first_complete_hour(cls, value: datetime) -> datetime:
        return align_to_next_hour(value)

    @staticmethod
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


def latest_completed_hour(value: datetime) -> datetime:
    """Return the UTC boundary of the latest completed hourly interval."""
    return HomeAssistantEnergyAggregator._latest_completed_hour(value)


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
