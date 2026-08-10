"""Home Assistant household-load provider."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
    HomeAssistantConfiguration,
    HouseholdLoadEntityConfiguration,
)
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_SOURCE_ID,
    HouseholdLoadData,
    SourceMetadata,
)


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant data cannot be imported safely."""


class HomeAssistantLoadImporter:
    """Retrieve and normalize household-load history from Home Assistant.

    The importer performs one bounded request per ``fetch`` call. Polling,
    scheduling, caching, persistence, and freshness policy enforcement are
    intentionally owned by a later orchestration layer.
    """

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._client = client

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HouseholdLoadData:
        """Fetch hourly load for ``[start_time, end_time)``.

        ``history_lookback_seconds`` requests an earlier state so a value can
        be carried forward when the first requested hour has no state change.
        When ``end_time`` is omitted, the period ends at the latest completed
        UTC hour.
        """
        retrieved_at = self._as_utc(now or datetime.now(timezone.utc))
        effective_end_time = end_time or self._latest_completed_hour(retrieved_at)
        self._validate_period(start_time, effective_end_time, history_lookback_seconds)
        start = self._as_utc(start_time)
        end = self._as_utc(effective_end_time)
        contributions: list[tuple[list[float], str]] = []
        observations: list[datetime] = []
        for entity in self.configuration.household_load_entities or []:
            query_start = start - timedelta(seconds=history_lookback_seconds)
            response = self._request(
                self._history_url(query_start, end, entity.entity_id), entity
            )
            records = self._parse_history(response, entity)
            values, latest_observation = self._normalize_records(
                records, start, end, entity
            )
            contributions.append((values, entity.operation))
            observations.append(latest_observation)

        if not contributions:
            raise HomeAssistantError(
                "no Home Assistant household-load energy entities are configured"
            )
        value_count = len(contributions[0][0])
        if any(len(values) != value_count for values, _ in contributions):
            raise HomeAssistantError(
                "Home Assistant household-load entities returned misaligned hourly "
                "series"
            )
        values = [0.0] * value_count
        for contribution, operation in contributions:
            sign = 1.0 if operation == "add" else -1.0
            for index, value in enumerate(contribution):
                values[index] += sign * value
        for index, value in enumerate(values):
            if not math.isfinite(value) or value < -1e-9:
                raise HomeAssistantError(
                    "combined Home Assistant household-load data contains a "
                    f"negative or non-finite value at hour {index}; check add and "
                    "subtract operations"
                )
            values[index] = max(0.0, value)

        return HouseholdLoadData(
            schema_version="1",
            start_time=self._as_utc(start_time),
            interval_minutes=60,
            load_kw=tuple(values),
            unit="kW",
            source=SourceMetadata(
                provider="home-assistant",
                entity_id=HOUSEHOLD_LOAD_SOURCE_ID,
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=min(observations),
        )

    def is_fresh(
        self,
        data: HouseholdLoadData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Check whether a poll result is within the configured age threshold."""
        threshold = self.configuration.max_data_age_seconds
        if threshold is None:
            return True
        current_time = self._as_utc(now or datetime.now(timezone.utc))
        age_seconds = (current_time - data.latest_observation_at).total_seconds()
        return age_seconds < threshold

    def _history_url(
        self, start_time: datetime, end_time: datetime, entity_id: str
    ) -> str:
        base_url = str(self.configuration.base_url).rstrip("/")
        encoded_start = quote(self._as_utc(start_time).isoformat(), safe="")
        return (
            f"{base_url}/api/history/period/{encoded_start}"
            f"?end_time={quote(self._as_utc(end_time).isoformat(), safe='')}"
            f"&filter_entity_id={quote(entity_id)}"
            "&minimal_response"
        )

    def _request(self, url: str, entity: HouseholdLoadEntityConfiguration) -> Any:
        headers = {
            "Authorization": f"Bearer {self.configuration.token.get_secret_value()}",
            "Accept": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.get(
                    url,
                    headers=headers,
                    timeout=self.configuration.timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self.configuration.timeout_seconds) as client:
                    response = client.get(url, headers=headers)
        except httpx.TimeoutException as error:
            raise HomeAssistantError(
                "Home Assistant request timed out; check the endpoint and timeout"
            ) from error
        except httpx.RequestError as error:
            raise HomeAssistantError(
                f"Home Assistant request failed: {error}"
            ) from error

        if response.status_code in (401, 403):
            raise HomeAssistantError(
                "Home Assistant authentication failed; check the configured token"
            )
        if response.status_code == 404:
            raise HomeAssistantError(
                "Home Assistant household-load history was not found for "
                f"{entity.entity_id}; check the configured entity ID and endpoint"
            )
        if response.is_error:
            raise HomeAssistantError(
                "Home Assistant returned HTTP "
                f"{response.status_code} while retrieving household-load history"
            )
        try:
            return response.json()
        except ValueError as error:
            raise HomeAssistantError(
                "Home Assistant returned malformed JSON for household-load history"
            ) from error

    def _parse_history(
        self, payload: Any, entity: HouseholdLoadEntityConfiguration
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise HomeAssistantError(
                f"Home Assistant history for {entity.entity_id} must contain one "
                "entity series"
            )
        if not payload or (len(payload) == 1 and not payload[0]):
            raise HomeAssistantError(
                f"Home Assistant returned no household-load history for "
                f"{entity.entity_id}"
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
        entity: HouseholdLoadEntityConfiguration,
    ) -> tuple[list[float], datetime]:
        parsed: list[tuple[datetime, float]] = []
        previous_unit: str | None = None
        for record in records:
            timestamp = self._parse_timestamp(
                record.get("last_updated", record.get("last_changed"))
            )
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
                    f"non-numeric value at {timestamp.isoformat()}: {state!r}"
                ) from error
            if not math.isfinite(value) or value < 0:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} contains an invalid "
                    f"value at {timestamp.isoformat()}: {state!r}"
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
            parsed.append((timestamp, value))
            previous_unit = normalized_unit

        parsed.sort(key=lambda record: record[0])
        if not parsed:
            raise HomeAssistantError(
                f"Home Assistant returned no usable history for {entity.entity_id}"
            )
        start = self._as_utc(start_time)
        end = self._as_utc(end_time)
        hour_count = int((end - start).total_seconds() // 3600)
        factor = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}[entity.unit]
        timestamps = [timestamp for timestamp, _ in parsed]
        if len(timestamps) != len(set(timestamps)):
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} contains duplicate "
                "timestamps"
            )
        if entity.reading_type == "interval" and any(
            timestamp.minute or timestamp.second or timestamp.microsecond
            for timestamp in timestamps
        ):
            raise HomeAssistantError(
                f"Home Assistant entity {entity.entity_id} has misaligned "
                "timestamps; energy readings must be hourly"
            )

        values_by_timestamp = {timestamp: value * factor for timestamp, value in parsed}
        if entity.reading_type == "interval":
            expected_timestamps = [
                start + timedelta(hours=hour) for hour in range(hour_count)
            ]
            missing = [
                timestamp
                for timestamp in expected_timestamps
                if timestamp not in values_by_timestamp
            ]
            if missing:
                raise HomeAssistantError(
                    f"Home Assistant entity {entity.entity_id} is missing an "
                    f"interval at {missing[0].isoformat()}"
                )
            return (
                [values_by_timestamp[timestamp] for timestamp in expected_timestamps],
                max(
                    timestamp
                    for timestamp in timestamps
                    if timestamp in expected_timestamps
                ),
            )

        for previous, following in zip(parsed, parsed[1:]):
            if following[1] < previous[1]:
                raise HomeAssistantError(
                    f"Home Assistant cumulative energy entity {entity.entity_id} "
                    f"reset between {previous[0].isoformat()} and "
                    f"{following[0].isoformat()}"
                )
        expected_boundaries = [
            start + timedelta(hours=hour) for hour in range(hour_count + 1)
        ]
        boundary_values: list[float] = []
        boundary_observations: list[datetime] = []
        record_index = 0
        current_boundary: tuple[datetime, float] | None = None
        for boundary in expected_boundaries:
            while record_index < len(parsed) and parsed[record_index][0] <= boundary:
                current_boundary = parsed[record_index]
                record_index += 1
            if current_boundary is None:
                raise HomeAssistantError(
                    f"Home Assistant cumulative energy entity {entity.entity_id} is "
                    f"missing a usable value at {boundary.isoformat()}; increase "
                    "history lookback or history coverage"
                )
            boundary_values.append(current_boundary[1] * factor)
            boundary_observations.append(current_boundary[0])
        values = [
            later - earlier
            for earlier, later in zip(boundary_values, boundary_values[1:])
        ]
        return values, max(boundary_observations)

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise HomeAssistantError(
                "Home Assistant household-load history has a missing timestamp"
            )
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as error:
            raise HomeAssistantError(
                f"Home Assistant returned an invalid timestamp: {value!r}"
            ) from error
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise HomeAssistantError(
                "Home Assistant household-load timestamps must include a timezone"
            )
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise HomeAssistantError(
                "Home Assistant import times must include a timezone"
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _latest_completed_hour(value: datetime) -> datetime:
        """Return the UTC boundary of the latest completed hourly interval."""
        return value.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _validate_period(
        start_time: datetime,
        end_time: datetime,
        history_lookback_seconds: float,
    ) -> None:
        start = HomeAssistantLoadImporter._as_utc(start_time)
        end = HomeAssistantLoadImporter._as_utc(end_time)
        if end <= start:
            raise HomeAssistantError("household-load end_time must be after start_time")
        duration_seconds = (end - start).total_seconds()
        if duration_seconds % 3600 != 0:
            raise HomeAssistantError(
                "household-load requested period must contain whole hourly intervals"
            )
        if start.minute or start.second or start.microsecond:
            raise HomeAssistantError(
                "household-load start_time must be aligned to the hour"
            )
        if not math.isfinite(history_lookback_seconds) or history_lookback_seconds < 0:
            raise HomeAssistantError(
                "history_lookback_seconds must be finite and non-negative"
            )
