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
# aWATTar publishes the EPEX day-ahead prices for the next day at 14:00 local
# time, so a request that reaches two days ahead always covers tomorrow.
PRICE_LOOKAHEAD = timedelta(hours=48)


class AwattarError(RuntimeError):
    """Raised when aWATTar data cannot be imported safely."""


class AwattarImporter:
    """Fetch and normalize German aWATTar day-ahead prices."""

    def __init__(
        self, configuration: AwattarConfiguration, client: httpx.Client | None = None
    ) -> None:
        self.configuration = configuration
        self._http = JsonHttpClient(client)

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> ElectricityPriceData:
        retrieved_at = now or datetime.now(timezone.utc)
        try:
            start, end = _request_window(start_time, end_time)
            data = _normalize(self._retrieve(start, end), start, end, retrieved_at)
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

    def _retrieve(self, start: datetime, end: datetime) -> Any:
        """Retrieve the German market-data document for one half-open window.

        ``start`` is inclusive and ``end`` is exclusive. Both must be timezone-aware
        and are sent as epoch milliseconds, the format aWATTar expects.
        """
        return self._http.get_json(
            str(self.configuration.base_url),
            params={
                "start": _epoch_milliseconds(start),
                "end": _epoch_milliseconds(end),
            },
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


def _epoch_milliseconds(value: datetime) -> int:
    return round(value.timestamp() * 1000)


def _as_utc(
    value: datetime, message: str = "aWATTar price times must include a timezone"
) -> datetime:
    return as_utc(value, error_factory=AwattarError, message=message)


def _request_window(
    start_time: datetime, end_time: datetime | None
) -> tuple[datetime, datetime]:
    """Validate the requested period and return its UTC half-open window.

    Without an ``end_time`` the window reaches ``PRICE_LOOKAHEAD`` past the
    start. The returned end is the value used both for the request and for
    selecting intervals.
    """
    start = _as_utc(start_time)
    end = _as_utc(end_time) if end_time is not None else None
    validate_hourly_period(
        start,
        end,
        error_factory=AwattarError,
        start_message="aWATTar start_time must be aligned to the hour",
        end_message="aWATTar end_time must be aligned to the hour",
        order_message="aWATTar end_time must be after start_time",
        check_order_first=False,
    )
    return start, end if end is not None else start + PRICE_LOOKAHEAD


def _normalize(
    payload: Any, start: datetime, end: datetime, retrieved_at: datetime
) -> ElectricityPriceData:
    """Validate market intervals and convert EUR/MWh to EUR/kWh."""
    retrieved = _as_utc(retrieved_at, "aWATTar retrieval time must include a timezone")
    intervals = _parse_intervals(payload)
    if not intervals:
        raise AwattarError("aWATTar response contains no market-data intervals")

    effective_start = max(start, align_to_next_hour(retrieved))
    selected = [
        interval for interval in intervals if effective_start <= interval[0] < end
    ]
    if not selected:
        raise AwattarError("aWATTar response contains no usable future prices")

    market_prices = tuple(interval[2] for interval in selected)
    return ElectricityPriceData(
        schema_version="1",
        timestamps=tuple(interval[0] for interval in selected),
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
        start = _timestamp(raw_start, index, "start")
        end = _timestamp(raw_end, index, "end")
        if end - start != _HOUR:
            raise AwattarError(f"aWATTar interval {index} must cover exactly one hour")
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
            raise AwattarError(f"aWATTar interval {index} marketprice must be finite")
        normalized = price / 1000
        if not -100 <= normalized <= 100:
            raise AwattarError(
                f"aWATTar interval {index} marketprice is outside EUR/kWh limits"
            )
        parsed.append((start, end, normalized))
        previous_end = end
    return parsed


def _timestamp(value: Any, index: int, label: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AwattarError(
            f"aWATTar interval {index} {label}_timestamp must be numeric"
        )
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise AwattarError(
            f"aWATTar interval {index} {label}_timestamp is invalid"
        ) from error
