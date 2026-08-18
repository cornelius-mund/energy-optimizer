"""aWATTar Germany EPEX Spot electricity-price provider."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from energy_optimizer.config import AwattarConfiguration
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    ELECTRICITY_PRICE_SOURCE_ID,
    ElectricityPriceData,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    as_utc,
    is_fresh,
    validate_hourly_period,
)

logger = logging.getLogger(__name__)
_HOUR = timedelta(hours=1)


class AwattarError(RuntimeError):
    """Raised when aWATTar data cannot be imported safely."""


class AwattarClient:
    """Retrieve the unauthenticated aWATTar market-data response."""

    def __init__(
        self,
        configuration: AwattarConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._http = JsonHttpClient(client)

    def fetch(self) -> Any:
        """Retrieve the current German market-data document."""
        return self._http.get_json(
            str(self.configuration.base_url),
            headers={"Accept": "application/json"},
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=AwattarError,
            timeout_message="aWATTar request timed out; check the endpoint and timeout",
            transport_message="aWATTar request failed: transport error",
            malformed_message="aWATTar returned malformed JSON",
            status_error=lambda status: (
                AwattarError(f"aWATTar returned HTTP {status} while retrieving prices")
                if status >= 400
                else None
            ),
            log_event="awattar_request",
            component="awattar",
            operation="marketdata",
            log_context="source=awattar.de",
        )


class AwattarNormalizer:
    """Validate market intervals and convert EUR/MWh to EUR/kWh."""

    def normalize(
        self,
        payload: Any,
        start_time: datetime,
        end_time: datetime | None,
        retrieved_at: datetime,
    ) -> ElectricityPriceData:
        start = as_utc(
            start_time,
            error_factory=AwattarError,
            message="aWATTar price times must include a timezone",
        )
        retrieved = as_utc(
            retrieved_at,
            error_factory=AwattarError,
            message="aWATTar retrieval time must include a timezone",
        )
        end = (
            as_utc(
                end_time,
                error_factory=AwattarError,
                message="aWATTar price times must include a timezone",
            )
            if end_time is not None
            else None
        )
        validate_hourly_period(
            start,
            end,
            error_factory=AwattarError,
            start_message="aWATTar start_time must be aligned to the hour",
            end_message="aWATTar end_time must be aligned to the hour",
            order_message="aWATTar end_time must be after start_time",
            check_order_first=False,
        )
        intervals = self._parse_intervals(payload)
        if not intervals:
            raise AwattarError("aWATTar response contains no market-data intervals")

        effective_start = max(start, align_to_next_hour(retrieved))
        selected = [
            interval
            for interval in intervals
            if interval[0] >= effective_start and (end is None or interval[0] < end)
        ]
        if not selected:
            raise AwattarError("aWATTar response contains no usable future prices")
        if any(
            later[0] - earlier[0] != _HOUR
            for earlier, later in zip(selected, selected[1:])
        ):
            raise AwattarError(
                "aWATTar price intervals must be contiguous in the requested range"
            )

        timestamps = tuple(interval[0] for interval in selected)
        market_prices = tuple(interval[2] for interval in selected)
        return ElectricityPriceData(
            schema_version="1",
            timestamps=timestamps,
            interval_minutes=60,
            # aWATTar provides one market price, which applies to both directions.
            import_price_eur_per_kwh=market_prices,
            export_price_eur_per_kwh=market_prices,
            unit="EUR/kWh",
            source=SourceMetadata(
                provider="awattar.de", entity_id=ELECTRICITY_PRICE_SOURCE_ID
            ),
            retrieved_at=retrieved,
            expires_at=selected[-1][1],
        )

    @staticmethod
    def _parse_intervals(payload: Any) -> list[tuple[datetime, datetime, float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise AwattarError("aWATTar response must contain a data array")
        parsed: list[tuple[datetime, datetime, float]] = []
        previous_end: datetime | None = None
        for index, item in enumerate(payload["data"]):
            if not isinstance(item, dict):
                raise AwattarError(f"aWATTar interval {index} must be an object")
            try:
                raw_start = item["start_timestamp"]
                raw_end = item["end_timestamp"]
                raw_price = item["marketprice"]
                unit = item["unit"]
            except KeyError as error:
                raise AwattarError(
                    f"aWATTar interval {index} is missing {error.args[0]}"
                ) from error
            if unit != "Eur/MWh":
                raise AwattarError(
                    f"aWATTar interval {index} has unsupported unit {unit!r}"
                )
            start = AwattarNormalizer._timestamp(raw_start, index, "start")
            end = AwattarNormalizer._timestamp(raw_end, index, "end")
            if end - start != _HOUR:
                raise AwattarError(
                    f"aWATTar interval {index} must cover exactly one hour"
                )
            if previous_end is not None and start != previous_end:
                raise AwattarError(
                    "aWATTar intervals must be ordered, non-overlapping, and contiguous"
                )
            try:
                price = float(raw_price)
            except (TypeError, ValueError) as error:
                raise AwattarError(
                    f"aWATTar interval {index} marketprice must be numeric"
                ) from error
            if isinstance(raw_price, bool) or not math.isfinite(price):
                raise AwattarError(
                    f"aWATTar interval {index} marketprice must be finite"
                )
            normalized = price / 1000
            if not -100 <= normalized <= 100:
                raise AwattarError(
                    f"aWATTar interval {index} marketprice is outside EUR/kWh limits"
                )
            parsed.append((start, end, normalized))
            previous_end = end
        return parsed

    @staticmethod
    def _timestamp(value: Any, index: int, label: str) -> datetime:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AwattarError(
                f"aWATTar interval {index} {label}_timestamp must be numeric"
            )
        try:
            timestamp = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise AwattarError(
                f"aWATTar interval {index} {label}_timestamp is invalid"
            ) from error
        return timestamp


class AwattarImporter:
    """Fetch and normalize German aWATTar day-ahead prices."""

    def __init__(
        self,
        configuration: AwattarConfiguration,
        client: httpx.Client | None = None,
        normalizer: AwattarNormalizer | None = None,
    ) -> None:
        self.configuration = configuration
        self._client = AwattarClient(configuration, client)
        self._normalizer = normalizer or AwattarNormalizer()

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> ElectricityPriceData:
        retrieved_at = now or datetime.now(timezone.utc)
        try:
            data = self._normalizer.normalize(
                self._client.fetch(), start_time, end_time, retrieved_at
            )
        except Exception as error:
            logger.error(
                "event=provider_fetch_failed component=awattar operation=fetch "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        logger.info(
            "event=provider_fetch_succeeded component=awattar operation=fetch "
            "start_time=%s end_time=%s record_count=%s",
            data.timestamps[0],
            data.expires_at,
            len(data.timestamps),
        )
        return data

    def is_fresh(
        self, data: ElectricityPriceData, *, now: datetime | None = None
    ) -> bool:
        return is_fresh(
            data.retrieved_at,
            self.configuration.max_data_age_seconds,
            expires_at=data.expires_at,
            now=now,
            error_factory=AwattarError,
            now_message="aWATTar freshness times must include a timezone",
        )
