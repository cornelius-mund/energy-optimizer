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
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_SOURCE_ID,
    HouseholdLoadData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import as_utc, parse_aware_timestamp

logger = logging.getLogger(__name__)


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
        self._http = JsonHttpClient(client)

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
        contributions: list[tuple[list[float], str, datetime]] = []
        observations: list[datetime] = []
        for entity in self.configuration.household_load_entities or []:
            query_start = start - timedelta(seconds=history_lookback_seconds)
            response = self._request(
                self._history_url(query_start, end, entity.entity_id),
                entity,
                query_start,
                end,
            )
            records = self._parse_history(response, entity)
            values, entity_start, latest_observation = self._normalize_records(
                records, start, end, entity
            )
            contributions.append((values, entity.operation, entity_start))
            observations.append(latest_observation)

        if not contributions:
            raise HomeAssistantError(
                "no Home Assistant household-load energy entities are configured"
            )
        aggregate_start = max(start for _, _, start in contributions)
        if aggregate_start >= end:
            raise HomeAssistantError(
                "Home Assistant household-load entities have no complete hourly "
                "history in the requested period"
            )
        value_count = int((end - aggregate_start).total_seconds() // 3600)
        values = [0.0] * value_count
        for contribution, operation, contribution_start in contributions:
            offset = int((aggregate_start - contribution_start).total_seconds() // 3600)
            aligned = contribution[offset : offset + value_count]
            if len(aligned) != value_count:
                raise HomeAssistantError(
                    "Home Assistant household-load entities returned misaligned "
                    "hourly series"
                )
            sign = 1.0 if operation == "add" else -1.0
            for index, value in enumerate(aligned):
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
            start_time=aggregate_start,
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

    def _request(
        self,
        url: str,
        entity: HouseholdLoadEntityConfiguration,
        start_time: datetime,
        end_time: datetime,
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
                    "Home Assistant household-load history was not found; check the "
                    "configured entity ID and endpoint"
                )
            if status >= 400:
                return HomeAssistantError(
                    "Home Assistant returned HTTP "
                    f"{status} while retrieving household-load history"
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
                "Home Assistant returned malformed JSON for household-load history"
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
    ) -> tuple[list[float], datetime, datetime]:
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
            elapsed_seconds = (timestamp - effective_start).total_seconds()
            if elapsed_seconds > 0:
                hour = math.ceil(elapsed_seconds / 3600) - 1
                values[hour] += delta * factor
            previous_value = value
            previous_reset = reset
            latest_observation = timestamp
        return values, effective_start, latest_observation

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        return parse_aware_timestamp(
            value,
            error_factory=HomeAssistantError,
            missing_message=(
                "Home Assistant household-load history has a missing timestamp"
            ),
            invalid_message=lambda raw: (
                f"Home Assistant returned an invalid timestamp: {raw!r}"
            ),
            naive_message=(
                "Home Assistant household-load timestamps must include a timezone"
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
        """Return the UTC boundary of the latest completed hourly interval."""
        return value.replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _next_hour(value: datetime) -> datetime:
        """Return the first UTC hour that starts after an observation."""
        return value.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    @classmethod
    def _first_complete_hour(cls, value: datetime) -> datetime:
        """Return the first aligned hour that follows an unbootstrapped baseline."""
        aligned = value.replace(minute=0, second=0, microsecond=0)
        return aligned if value == aligned else cls._next_hour(value)

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
