"""Home Assistant household-load provider."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


class HomeAssistantError(RuntimeError):
    """Raised when Home Assistant data cannot be imported safely."""


logger = logging.getLogger(__name__)


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
        logger.debug(
            "event=provider_fetch_started component=home_assistant operation=fetch "
            "start_time=%s end_time=%s history_lookback_seconds=%s entity_count=%s",
            start_time,
            end_time,
            history_lookback_seconds,
            len(self.configuration.household_load_entities or []),
        )
        try:
            data = self._fetch(
                start_time,
                end_time,
                history_lookback_seconds,
                now=now,
            )
        except Exception as error:
            if isinstance(error, HomeAssistantError) and any(
                marker in str(error) for marker in ("unknown", "unavailable")
            ):
                logger.warning(
                    "event=provider_data_degraded component=home_assistant "
                    "operation=fetch error_type=%s entity_count=%s error=%s",
                    error.__class__.__name__,
                    len(self.configuration.household_load_entities or []),
                    error,
                )
            logger.error(
                "event=provider_fetch_failed component=home_assistant "
                "operation=fetch "
                "error_type=%s entity_count=%s",
                error.__class__.__name__,
                len(self.configuration.household_load_entities or []),
                exc_info=True,
            )
            raise
        logger.info(
            "event=provider_fetch_succeeded component=home_assistant operation=fetch "
            "start_time=%s end_time=%s record_count=%s entity_count=%s",
            data.start_time,
            end_time,
            len(data.load_kw),
            len(self.configuration.household_load_entities or []),
        )
        return data

    def _fetch(
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
            logger.debug(
                "event=provider_request_started component=home_assistant "
                "operation=history_request entity_id=%s start_time=%s end_time=%s",
                entity.entity_id,
                query_start,
                end,
            )
            response = self._request(
                self._history_url(query_start, end, entity.entity_id), entity
            )
            records = self._parse_history(response, entity)
            logger.debug(
                "event=provider_response_parsed component=home_assistant "
                "operation=history_request entity_id=%s record_count=%s",
                entity.entity_id,
                len(records),
            )
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
        raw_records: list[tuple[datetime, dict[str, Any]]] = []
        skipped_records: list[datetime] = []
        for record in records:
            timestamp = self._parse_timestamp(
                record.get("last_updated", record.get("last_changed"))
            )
            state = record.get("state")
            if isinstance(state, str) and state in {"unknown", "unavailable"}:
                skipped_records.append(timestamp)
                continue
            raw_records.append((timestamp, record))

        if skipped_records:
            first_skipped = min(skipped_records)
            last_skipped = max(skipped_records)
            logger.warning(
                "Home Assistant entity %s has %d unknown or unavailable history "
                "samples between %s and %s; skipped them without assigning energy",
                entity.entity_id,
                len(skipped_records),
                first_skipped.isoformat(),
                last_skipped.isoformat(),
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
                    normalized_reset = self._parse_timestamp(raw_reset)
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
        hour_count = int((end - start).total_seconds() // 3600)
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
        if baseline_index < 0:
            raise HomeAssistantError(
                f"Home Assistant cumulative energy entity {entity.entity_id} has no "
                f"usable value at or before {start.isoformat()}; increase history "
                "lookback"
            )
        baseline = parsed[baseline_index]
        values = [0.0] * hour_count
        previous_value = baseline[1]
        previous_reset = baseline[2]
        latest_observation = baseline[0]
        for timestamp, value, reset in parsed[baseline_index + 1 :]:
            if timestamp > end:
                break
            if value >= previous_value:
                delta = value - previous_value
            elif entity.state_class == "total_increasing":
                delta = value
            elif reset != previous_reset:
                delta = value
            else:
                raise HomeAssistantError(
                    f"Home Assistant total entity {entity.entity_id} decreased "
                    "without a changed last_reset timestamp"
                )
            elapsed_seconds = (timestamp - start).total_seconds()
            if elapsed_seconds > 0:
                hour = math.ceil(elapsed_seconds / 3600) - 1
                values[hour] += delta * factor
            previous_value = value
            previous_reset = reset
            latest_observation = timestamp
        return values, latest_observation

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
