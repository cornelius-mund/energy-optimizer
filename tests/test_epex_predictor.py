"""Tests for the EPEX Predictor price-forecast provider."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from energy_optimizer.config import EpexPredictorConfiguration
from energy_optimizer.providers.epex_predictor import (
    EpexPredictorError,
    EpexPredictorImporter,
)

NOW = datetime(2026, 11, 8, 18, 20, tzinfo=UTC)
RUNNING_HOUR = datetime(2026, 11, 8, 18, tzinfo=UTC)
ONE_HOUR = timedelta(hours=1)

Handler = Callable[[httpx.Request], httpx.Response]


def configuration(**overrides: Any) -> EpexPredictorConfiguration:
    return EpexPredictorConfiguration.model_validate(
        {"base_url": "http://epex.test", "timeout_seconds": 5, **overrides}
    )


def payload(
    prices_eur_per_mwh: list[float], first: datetime = RUNNING_HOUR
) -> dict[str, Any]:
    return {
        "prices": [
            {
                "startsAt": (first + ONE_HOUR * index).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "total": price,
            }
            for index, price in enumerate(prices_eur_per_mwh)
        ],
        "knownUntil": (first + ONE_HOUR).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def importer(handler: Handler, **overrides: Any) -> EpexPredictorImporter:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EpexPredictorImporter(configuration(**overrides), client)


def answering(body: Any, status: int = 200) -> Handler:
    return lambda _: httpx.Response(status, json=body)


def test_future_hours_are_imported_in_eur_per_kwh() -> None:
    data = importer(answering(payload([100.0, 250.0, 80.0]))).fetch(now=NOW)

    assert data.timestamps == (RUNNING_HOUR + ONE_HOUR, RUNNING_HOUR + 2 * ONE_HOUR)
    assert data.import_price_eur_per_kwh == pytest.approx((0.25, 0.08))
    assert data.export_price_eur_per_kwh == data.import_price_eur_per_kwh
    assert data.expires_at == RUNNING_HOUR + 3 * ONE_HOUR
    assert data.source.provider == "epex-predictor"


def test_the_request_asks_for_hourly_utc_prices_of_the_configured_region() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload([100.0, 120.0]))

    importer(handler, region="AT", forecast_hours=72).fetch(now=NOW)

    (request,) = requests
    assert request.url.path == "/prices"
    assert dict(request.url.params) == {
        "region": "AT",
        "hours": "72",
        "unit": "EUR_PER_MWH",
        "hourly": "true",
        "timezone": "UTC",
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"data": []}, "prices array"),
        (payload([100.0]), "no future prices"),
        ({"prices": [{"startsAt": "yesterday", "total": 1.0}]}, "startsAt is invalid"),
        ({"prices": [{"total": 1.0}]}, "needs a startsAt"),
        (
            {"prices": [{"startsAt": "2026-11-08T19:15:00Z", "total": 1.0}]},
            "full hour",
        ),
        (
            {"prices": [{"startsAt": "2026-11-08T19:00:00Z", "total": "high"}]},
            "must be numeric",
        ),
        (
            {"prices": [{"startsAt": "2026-11-08T19:00:00Z", "total": 1e9}]},
            "outside EUR/kWh limits",
        ),
        (
            {
                "prices": [
                    {"startsAt": "2026-11-08T19:00:00Z", "total": 1.0},
                    {"startsAt": "2026-11-08T21:00:00Z", "total": 1.0},
                ]
            },
            "contiguous",
        ),
    ],
)
def test_an_unusable_answer_is_rejected(body: Any, message: str) -> None:
    with pytest.raises(EpexPredictorError, match=message):
        importer(answering(body)).fetch(now=NOW)


def test_an_http_error_is_reported() -> None:
    with pytest.raises(EpexPredictorError, match="HTTP 503"):
        importer(answering({"detail": "down"}, status=503)).fetch(now=NOW)


def test_the_forecast_turns_stale_after_its_maximum_age() -> None:
    forecast_importer = importer(answering(payload([100.0] * 10)))
    data = forecast_importer.fetch(now=NOW)

    assert forecast_importer.is_fresh(data, now=NOW + timedelta(hours=5))
    assert not forecast_importer.is_fresh(data, now=NOW + timedelta(hours=7))
