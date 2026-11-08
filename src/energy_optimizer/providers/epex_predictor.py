"""EPEX Predictor day-ahead price forecast for the days after the published prices."""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from energy_optimizer.config import EpexPredictorConfiguration
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
)

logger = logging.getLogger(__name__)
ONE_HOUR = timedelta(hours=1)
EUR_PER_MWH_PER_EUR_PER_KWH = 1000
PRICE_LIMIT_EUR_PER_KWH = 100
PROVIDER_NAME = "epex-predictor"


class EpexPredictorError(RuntimeError):
    """Raised when the EPEX Predictor forecast cannot be imported safely."""


class EpexPredictorImporter:
    """Fetch hourly market-price forecasts in EUR/kWh from EPEX Predictor."""

    def __init__(
        self,
        configuration: EpexPredictorConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        self.configuration = configuration
        self._http = JsonHttpClient(client)

    def fetch(self, *, now: datetime | None = None) -> ElectricityPriceData:
        retrieved_at = now or datetime.now(timezone.utc)
        try:
            data = _normalize(self._retrieve(), _as_utc(retrieved_at))
        except Exception as error:
            logger.warning(
                "event=provider_fetch_failed component=epex_predictor operation=fetch "
                "error_type=%s error=%s",
                error.__class__.__name__,
                error,
            )
            raise
        logger.info(
            "event=provider_fetch_succeeded component=epex_predictor operation=fetch "
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
            error_factory=EpexPredictorError,
            now_message="EPEX Predictor freshness times must include a timezone",
        )

    def _retrieve(self) -> Any:
        return self._http.get_json(
            str(self.configuration.base_url).rstrip("/") + "/prices",
            params={
                "region": self.configuration.region,
                "hours": self.configuration.forecast_hours,
                "unit": "EUR_PER_MWH",
                "hourly": "true",
                "timezone": "UTC",
            },
            headers={"Accept": "application/json"},
            timeout_seconds=self.configuration.timeout_seconds,
            error_factory=EpexPredictorError,
            timeout_message="EPEX Predictor request timed out",
            transport_message="EPEX Predictor request failed: transport error",
            malformed_message="EPEX Predictor returned malformed JSON",
            status_error=lambda status: (
                EpexPredictorError(f"EPEX Predictor returned HTTP {status}")
                if status >= 400
                else None
            ),
            log_event="epex_predictor_request",
            component="epex_predictor",
            operation="prices",
            log_context=f"region={self.configuration.region}",
        )


def _as_utc(value: datetime) -> datetime:
    return as_utc(
        value,
        error_factory=EpexPredictorError,
        message="EPEX Predictor times must include a timezone",
    )


def _normalize(payload: Any, retrieved_at: datetime) -> ElectricityPriceData:
    first_future_hour = align_to_next_hour(retrieved_at)
    hourly_prices = [
        (start, price)
        for start, price in _parse_prices(payload)
        if start >= first_future_hour
    ]
    if not hourly_prices:
        raise EpexPredictorError("EPEX Predictor returned no future prices")
    timestamps = tuple(start for start, _ in hourly_prices)
    market_prices = tuple(price for _, price in hourly_prices)
    return ElectricityPriceData(
        schema_version="1",
        timestamps=timestamps,
        interval_minutes=60,
        import_price_eur_per_kwh=market_prices,
        export_price_eur_per_kwh=market_prices,
        unit="EUR/kWh",
        source=SourceMetadata(
            provider=PROVIDER_NAME, entity_id=ELECTRICITY_PRICE_SOURCE_ID
        ),
        retrieved_at=retrieved_at,
        expires_at=timestamps[-1] + ONE_HOUR,
    )


def _parse_prices(payload: Any) -> list[tuple[datetime, float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise EpexPredictorError("EPEX Predictor response must contain a prices array")
    parsed: list[tuple[datetime, float]] = []
    for index, item in enumerate(payload["prices"]):
        start = _start_time(item, index)
        if parsed and start - parsed[-1][0] != ONE_HOUR:
            raise EpexPredictorError(
                "EPEX Predictor prices must be hourly, ordered and contiguous"
            )
        parsed.append((start, _price_eur_per_kwh(item, index)))
    return parsed


def _start_time(item: Any, index: int) -> datetime:
    if not isinstance(item, dict) or not isinstance(item.get("startsAt"), str):
        raise EpexPredictorError(f"EPEX Predictor price {index} needs a startsAt time")
    try:
        start = datetime.fromisoformat(item["startsAt"].replace("Z", "+00:00"))
    except ValueError as error:
        raise EpexPredictorError(
            f"EPEX Predictor price {index} startsAt is invalid"
        ) from error
    start = _as_utc(start)
    if start.minute or start.second or start.microsecond:
        raise EpexPredictorError(
            f"EPEX Predictor price {index} must start at a full hour"
        )
    return start


def _price_eur_per_kwh(item: dict[str, Any], index: int) -> float:
    raw_price = item.get("total")
    if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
        raise EpexPredictorError(f"EPEX Predictor price {index} total must be numeric")
    price = raw_price / EUR_PER_MWH_PER_EUR_PER_KWH
    if not math.isfinite(price) or abs(price) > PRICE_LIMIT_EUR_PER_KWH:
        raise EpexPredictorError(
            f"EPEX Predictor price {index} is outside EUR/kWh limits"
        )
    return price
