"""Shared Home Assistant energy-history retrieval and normalization."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
    HomeAssistantConfiguration,
    HomeAssistantEnergyEntityConfiguration,
)
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import IntervalQuality
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

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
    ) -> HomeAssistantEnergySeries:
        """Fetch, align, and combine all configured entity contributions."""
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
            if not math.isfinite(value) or value < -1e-9:
                raise HomeAssistantError(
                    f"combined Home Assistant {label} data contains a negative or "
                    f"non-finite value at hour {index}; check add and subtract "
                    "operations"
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
        headers = {
            "Authorization": f"Bearer {self.configuration.token.get_secret_value()}",
            "Accept": "application/json",
        }

        def status_error(status: int) -> Exception | None:
            if status in (401, 403):
                return HomeAssistantError(
                    "Home Assistant authentication failed; check the configured token"
                )
            if status == 404:
                return HomeAssistantError(
                    f"Home Assistant {label} history was not found for "
                    f"{entity.entity_id}; check the configured entity ID and endpoint"
                )
            if status >= 400:
                return HomeAssistantError(
                    f"Home Assistant returned HTTP {status} while retrieving "
                    f"{label} history"
                )
            return None

        return self._http.get_json(
            url,
            headers=headers,
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=HomeAssistantError,
            timeout_message=(
                "Home Assistant request timed out; check the endpoint and timeout"
            ),
            transport_message="Home Assistant request failed: transport error",
            malformed_message=(
                f"Home Assistant returned malformed JSON for {label} history"
            ),
            status_error=status_error,
            log_event="home_assistant_history_request",
            component="home_assistant",
            operation="history_request",
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
        previous_value = baseline[1]
        previous_reset = baseline[2]
        reset_baseline: float | None = None
        latest_observation = baseline[0]
        for timestamp, value, reset in parsed[baseline_index + 1 :]:
            if timestamp > end:
                break
            reset_marker_changed = reset != previous_reset
            if reset_marker_changed:
                delta = 0.0
                reset_baseline = previous_value
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
                reset_baseline = previous_value
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
                    "previous_value=%s current_value=%s reason=counter_decreased",
                    entity.entity_id,
                    timestamp.isoformat(),
                    previous_value,
                    value,
                )
            elif value >= previous_value:
                if reset_baseline is not None and math.isclose(
                    value, reset_baseline, rel_tol=0.01, abs_tol=0.001
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
                        reset_baseline,
                    )
                else:
                    delta = value - previous_value
                reset_baseline = None
            elif entity.state_class == "total":
                raise HomeAssistantError(
                    f"Home Assistant total entity {entity.entity_id} decreased "
                    "without a changed last_reset timestamp"
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
            if elapsed_seconds > 0:
                hour = math.ceil(elapsed_seconds / 3600) - 1
                values[hour] += delta * factor
            previous_value = value
            previous_reset = reset
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

    @staticmethod
    def _next_hour(value: datetime) -> datetime:
        return value.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    @classmethod
    def _first_complete_hour(cls, value: datetime) -> datetime:
        aligned = value.replace(minute=0, second=0, microsecond=0)
        return aligned if value == aligned else cls._next_hour(value)

    @staticmethod
    def _validate_period(
        start_time: datetime,
        end_time: datetime,
        history_lookback_seconds: float,
        label: str,
    ) -> None:
        if end_time <= start_time:
            raise HomeAssistantError(f"{label} end_time must be after start_time")
        duration_seconds = (end_time - start_time).total_seconds()
        if duration_seconds % 3600 != 0:
            raise HomeAssistantError(
                f"{label} requested period must contain whole hourly intervals"
            )
        if start_time.minute or start_time.second or start_time.microsecond:
            raise HomeAssistantError(f"{label} start_time must be aligned to the hour")
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
    if max_data_age_seconds is None:
        return True
    current_time = as_utc(
        now or datetime.now(timezone.utc),
        error_factory=HomeAssistantError,
        message="Home Assistant freshness times must include a timezone",
    )
    age_seconds = (
        current_time
        - as_utc(
            latest_observation_at,
            error_factory=HomeAssistantError,
            message="Home Assistant observations must include a timezone",
        )
    ).total_seconds()
    return age_seconds < max_data_age_seconds
