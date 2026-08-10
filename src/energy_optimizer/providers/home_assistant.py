"""Home Assistant household-load provider."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import HomeAssistantConfiguration
from energy_optimizer.providers.interfaces import (
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
        query_start = start_time - timedelta(seconds=history_lookback_seconds)
        url = self._history_url(query_start, effective_end_time)
        response = self._request(url)
        records = self._parse_history(response)
        values, latest_observation = self._normalize_records(
            records, start_time, effective_end_time
        )

        return HouseholdLoadData(
            schema_version="1",
            start_time=self._as_utc(start_time),
            interval_minutes=60,
            load_kw=tuple(values),
            unit="kW",
            source=SourceMetadata(
                provider="home-assistant",
                entity_id=self.configuration.household_load_entity_id,
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=latest_observation,
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

    def _history_url(self, start_time: datetime, end_time: datetime) -> str:
        base_url = str(self.configuration.base_url).rstrip("/")
        encoded_start = quote(self._as_utc(start_time).isoformat(), safe="")
        return (
            f"{base_url}/api/history/period/{encoded_start}"
            f"?end_time={quote(self._as_utc(end_time).isoformat(), safe='')}"
            f"&filter_entity_id={quote(self.configuration.household_load_entity_id)}"
            "&minimal_response"
        )

    def _request(self, url: str) -> Any:
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
                "Home Assistant household-load history was not found; check the "
                "configured entity ID and endpoint"
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

    def _parse_history(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise HomeAssistantError(
                "Home Assistant household-load history must contain one entity series"
            )
        if not payload or (len(payload) == 1 and not payload[0]):
            raise HomeAssistantError(
                "Home Assistant returned no household-load history for the configured "
                "entity"
            )
        if len(payload) != 1:
            raise HomeAssistantError(
                "Home Assistant household-load history must contain one entity series"
            )
        series = payload[0]
        if not isinstance(series, list):
            raise HomeAssistantError(
                "Home Assistant household-load history must contain a list of records"
            )
        if not all(isinstance(record, dict) for record in series):
            raise HomeAssistantError(
                "Home Assistant household-load history contains an invalid record"
            )
        return series

    def _normalize_records(
        self,
        records: list[dict[str, Any]],
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[list[float], datetime]:
        parsed: list[tuple[datetime, float, str]] = []
        for record in records:
            timestamp = self._parse_timestamp(
                record.get("last_updated", record.get("last_changed"))
            )
            state = record.get("state")
            if not isinstance(state, str) or state in {"unknown", "unavailable"}:
                raise HomeAssistantError(
                    "Home Assistant household-load history contains an unavailable "
                    f"value at {timestamp.isoformat()}"
                )
            try:
                value = float(state)
            except (TypeError, ValueError) as error:
                raise HomeAssistantError(
                    "Home Assistant household-load history contains a non-numeric "
                    f"value at {timestamp.isoformat()}: {state!r}"
                ) from error
            if not math.isfinite(value) or value < 0:
                raise HomeAssistantError(
                    "Home Assistant household-load history contains an invalid "
                    f"value at {timestamp.isoformat()}: {state!r}"
                )
            attributes = record.get("attributes")
            if isinstance(attributes, dict) and isinstance(
                attributes.get("unit_of_measurement"), str
            ):
                normalized_unit = attributes["unit_of_measurement"].strip()
            elif parsed:
                normalized_unit = parsed[-1][2]
            else:
                raise HomeAssistantError(
                    "Home Assistant household-load history is missing "
                    "unit_of_measurement"
                )
            if normalized_unit not in {"W", "kW"}:
                raise HomeAssistantError(
                    "Unsupported Home Assistant household-load unit "
                    f"{normalized_unit!r}; expected 'W' or 'kW'"
                )
            parsed.append((timestamp, value, normalized_unit))

        parsed.sort(key=lambda record: record[0])
        units = {record[2] for record in parsed}
        if len(units) != 1:
            raise HomeAssistantError(
                "Home Assistant household-load history changes units between records"
            )
        unit = units.pop()
        start = self._as_utc(start_time)
        end = self._as_utc(end_time)
        hour_count = int((end - start).total_seconds() // 3600)
        values: list[float] = []
        record_index = 0
        current: tuple[datetime, float, str] | None = None
        for hour in range(hour_count):
            bucket_start = start + timedelta(hours=hour)
            while (
                record_index < len(parsed) and parsed[record_index][0] <= bucket_start
            ):
                current = parsed[record_index]
                record_index += 1
            if current is None:
                raise HomeAssistantError(
                    "Home Assistant household-load history has no value at "
                    f"{bucket_start.isoformat()}; increase history lookback"
                )
            value = current[1] / 1000 if unit == "W" else current[1]
            values.append(value)

        latest_observation = current[0] if current is not None else None
        if latest_observation is None:
            raise HomeAssistantError(
                "Home Assistant household-load history has no usable value in the "
                "requested period"
            )
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
