"""Direct Forecast.Solar PV forecast provider."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from energy_optimizer.config import ForecastSolarConfiguration
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    PV_GENERATION_SOURCE_ID,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import (
    as_utc,
    is_fresh,
    validate_finite_non_negative,
    validate_hourly_period,
)

logger = logging.getLogger(__name__)
FORECAST_SOLAR_MIN_INTERVAL_SECONDS = 300


class ForecastSolarError(RuntimeError):
    """Raised when Forecast.Solar data cannot be imported safely."""


class ForecastSolarClient:
    """Retrieve one public Forecast.Solar estimate response."""

    def __init__(
        self,
        configuration: ForecastSolarConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._http = JsonHttpClient(client)

    def fetch(self) -> Any:
        """Retrieve the public estimate for the configured PV installation."""
        configuration = self.configuration
        url = (
            f"{str(configuration.base_url).rstrip('/')}/estimate/"
            f"{self._format(configuration.latitude)}/"
            f"{self._format(configuration.longitude)}/"
            f"{self._format(configuration.declination_degrees)}/"
            f"{self._format(configuration.azimuth_degrees)}/"
            f"{self._format(configuration.peak_power_kw)}"
        )

        def status_error(status: int) -> Exception | None:
            if status == 429:
                return ForecastSolarError(
                    "Forecast.Solar rate limit reached; reduce polling frequency"
                )
            if status >= 400:
                return ForecastSolarError(
                    f"Forecast.Solar returned HTTP {status} while retrieving a forecast"
                )
            return None

        return self._http.get_json(
            url,
            headers={"Accept": "application/json"},
            timeout_seconds=configuration.timeout_seconds,
            error_factory=ForecastSolarError,
            timeout_message=(
                "Forecast.Solar request timed out; check the endpoint and timeout"
            ),
            transport_message="Forecast.Solar request failed: transport error",
            malformed_message="Forecast.Solar returned malformed JSON",
            status_error=status_error,
            log_event="forecast_solar_request",
            component="forecast_solar",
            operation="estimate",
            log_context="source=forecast.solar",
        )

    @staticmethod
    def _format(value: float) -> str:
        """Format a validated numeric path parameter without needless decimals."""
        return format(value, "g")


class ForecastSolarNormalizer:
    """Convert Forecast.Solar period powers into hourly UTC generation."""

    def normalize(
        self,
        payload: Any,
        start_time: datetime,
        end_time: datetime | None,
        retrieved_at: datetime,
    ) -> PvGenerationData:
        start = as_utc(
            start_time,
            error_factory=ForecastSolarError,
            message="Forecast.Solar forecast times must include a timezone",
        )
        retrieved = as_utc(
            retrieved_at,
            error_factory=ForecastSolarError,
            message="Forecast.Solar retrieval time must include a timezone",
        )
        periods, source_timezone = self._parse_periods(payload)
        available_dates = {
            timestamp.astimezone(source_timezone).date() for timestamp, _ in periods
        }
        if end_time is None and len(available_dates) < 2:
            raise ForecastSolarError(
                "Forecast.Solar response must contain today and the following day"
            )
        end = (
            as_utc(
                end_time,
                error_factory=ForecastSolarError,
                message="Forecast.Solar forecast times must include a timezone",
            )
            if end_time is not None
            else self._available_end(periods, source_timezone)
        )
        validate_hourly_period(
            start,
            end,
            error_factory=ForecastSolarError,
            start_message="Forecast.Solar start_time must be aligned to the hour",
            end_message="Forecast.Solar end_time must be aligned to the hour",
            order_message="Forecast.Solar end_time must be after start_time",
        )
        required_dates = self._required_local_dates(start, end, source_timezone)
        missing_dates = required_dates - available_dates
        if missing_dates:
            missing = ", ".join(str(value) for value in sorted(missing_dates))
            raise ForecastSolarError(
                "Forecast.Solar response does not cover requested local date(s): "
                f"{missing}"
            )

        first_timestamp, first_energy = periods[0]
        if first_timestamp > start and first_energy > 1e-9:
            raise ForecastSolarError(
                "Forecast.Solar response starts after the requested period with "
                "non-zero generation"
            )
        hour_count = int((end - start).total_seconds() // 3600)
        energy_watt_hours = [0.0] * hour_count
        for (previous_time, _), (timestamp, period_wh) in zip(periods, periods[1:]):
            elapsed_seconds = (timestamp - previous_time).total_seconds()
            if elapsed_seconds <= 0:
                raise ForecastSolarError(
                    "Forecast.Solar watt timestamps must be unique and ascending"
                )
            if elapsed_seconds > 3600 and period_wh > 1e-9:
                raise ForecastSolarError(
                    "Forecast.Solar response contains an incomplete non-zero period"
                )
            overlap_start = max(previous_time, start)
            overlap_end = min(timestamp, end)
            if overlap_start >= overlap_end:
                continue
            self._add_period(
                energy_watt_hours,
                start,
                overlap_start,
                overlap_end,
                period_wh,
                elapsed_seconds,
            )

        generation_kw = tuple(energy / 1000 for energy in energy_watt_hours)
        return PvGenerationData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            generation_kw=generation_kw,
            unit="kW",
            source=SourceMetadata(
                provider="forecast.solar",
                entity_id=PV_GENERATION_SOURCE_ID,
            ),
            retrieved_at=retrieved,
            expires_at=end,
        )

    def _parse_periods(
        self, payload: Any
    ) -> tuple[list[tuple[datetime, float]], ZoneInfo]:
        if not isinstance(payload, dict):
            raise ForecastSolarError("Forecast.Solar response must be a JSON object")
        result = payload.get("result")
        message = payload.get("message")
        if not isinstance(result, dict) or not isinstance(message, dict):
            raise ForecastSolarError(
                "Forecast.Solar response must contain result and message objects"
            )
        watts_payload = result.get("watts")
        periods_payload = result.get("watt_hours_period")
        info = message.get("info")
        timezone_name = info.get("timezone") if isinstance(info, dict) else None
        if not isinstance(watts_payload, dict) or not watts_payload:
            raise ForecastSolarError(
                "Forecast.Solar response must contain a non-empty result.watts object"
            )
        if not isinstance(periods_payload, dict) or not periods_payload:
            raise ForecastSolarError(
                "Forecast.Solar response must contain a non-empty "
                "result.watt_hours_period object"
            )
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ForecastSolarError(
                "Forecast.Solar response is missing message.info.timezone"
            )
        try:
            source_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ForecastSolarError(
                f"Forecast.Solar returned an unknown timezone: {timezone_name!r}"
            ) from error

        parsed: list[tuple[datetime, float]] = []
        for raw_timestamp, raw_value in periods_payload.items():
            timestamp = self._parse_timestamp(raw_timestamp, source_timezone)
            value = validate_finite_non_negative(
                raw_value,
                error_factory=ForecastSolarError,
                label=f"Forecast.Solar energy at {raw_timestamp!r}",
            )
            parsed.append((timestamp, value))
        parsed.sort(key=lambda item: item[0])
        timestamps = [timestamp for timestamp, _ in parsed]
        if len(timestamps) != len(set(timestamps)):
            raise ForecastSolarError(
                "Forecast.Solar period timestamps must be unique and ascending"
            )
        return parsed, source_timezone

    @staticmethod
    def _parse_timestamp(value: Any, source_timezone: ZoneInfo) -> datetime:
        if not isinstance(value, str):
            raise ForecastSolarError("Forecast.Solar period timestamps must be strings")
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError as error:
            raise ForecastSolarError(
                f"Forecast.Solar returned an invalid timestamp: {value!r}"
            ) from error
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=source_timezone)
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _required_local_dates(
        start: datetime, end: datetime, source_timezone: ZoneInfo
    ) -> set[date]:
        local_start = start.astimezone(source_timezone).date()
        local_end = (end - timedelta(microseconds=1)).astimezone(source_timezone).date()
        dates: set[date] = set()
        current = local_start
        while current <= local_end:
            dates.add(current)
            current += timedelta(days=1)
        return dates

    @staticmethod
    def _available_end(
        periods: list[tuple[datetime, float]], source_timezone: ZoneInfo
    ) -> datetime:
        """Return the UTC end of the latest local forecast date."""
        latest_date = max(
            timestamp.astimezone(source_timezone).date() for timestamp, _ in periods
        )
        next_date = latest_date + timedelta(days=1)
        local_midnight = datetime.combine(
            next_date,
            datetime.min.time(),
            tzinfo=source_timezone,
        )
        return local_midnight.astimezone(timezone.utc)

    @staticmethod
    def _add_period(
        energy: list[float],
        start: datetime,
        period_start: datetime,
        period_end: datetime,
        energy_wh: float,
        full_period_seconds: float,
    ) -> None:
        cursor = period_start
        while cursor < period_end:
            hour_index = int((cursor - start).total_seconds() // 3600)
            hour_end = min(
                period_end,
                start + timedelta(hours=hour_index + 1),
            )
            energy[hour_index] += energy_wh * (
                (hour_end - cursor).total_seconds() / full_period_seconds
            )
            cursor = hour_end


class ForecastSolarImporter:
    """Fetch and normalize the free public Forecast.Solar forecast."""

    def __init__(
        self,
        configuration: ForecastSolarConfiguration,
        client: httpx.Client | None = None,
        normalizer: ForecastSolarNormalizer | None = None,
    ) -> None:
        self.configuration = configuration
        self._client = ForecastSolarClient(configuration, client)
        self._normalizer = normalizer or ForecastSolarNormalizer()

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> PvGenerationData:
        retrieved_at = now or datetime.now(timezone.utc)
        logger.debug(
            "event=provider_fetch_started component=forecast_solar operation=fetch "
            "start_time=%s end_time=%s",
            start_time,
            end_time,
        )
        try:
            data = self._normalizer.normalize(
                self._client.fetch(), start_time, end_time, retrieved_at
            )
        except Exception as error:
            logger.error(
                "event=provider_fetch_failed component=forecast_solar operation=fetch "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        logger.info(
            "event=provider_fetch_succeeded component=forecast_solar operation=fetch "
            "start_time=%s end_time=%s record_count=%s",
            data.start_time,
            data.expires_at,
            len(data.generation_kw),
        )
        return data

    def is_fresh(
        self,
        data: PvGenerationData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Check forecast coverage and optional retrieval age."""
        return is_fresh(
            data.retrieved_at,
            self.configuration.max_data_age_seconds,
            expires_at=data.expires_at,
            now=now,
            error_factory=ForecastSolarError,
            now_message="Forecast.Solar freshness times must include a timezone",
        )
