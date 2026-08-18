"""Tests for the aWATTar Germany electricity-price provider."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from energy_optimizer.config import AwattarConfiguration
from energy_optimizer.providers.awattar import AwattarError, AwattarImporter

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)


def configuration(**overrides: Any) -> AwattarConfiguration:
    values: dict[str, Any] = {"timeout_seconds": 5, "max_data_age_seconds": 7200}
    values.update(overrides)
    return AwattarConfiguration.model_validate(values)


def payload(
    *, prices: tuple[float, ...] = (100.0, 120.0), unit: str = "Eur/MWh"
) -> dict[str, object]:
    return {
        "data": [
            {
                "start_timestamp": int(
                    (START + timedelta(hours=index)).timestamp() * 1000
                ),
                "end_timestamp": int(
                    (START + timedelta(hours=index + 1)).timestamp() * 1000
                ),
                "marketprice": price,
                "unit": unit,
            }
            for index, price in enumerate(prices)
        ]
    }


def importer(
    handler: httpx.MockTransport,
    **overrides: Any,
) -> tuple[AwattarImporter, httpx.Client]:
    client = httpx.Client(transport=handler)
    return AwattarImporter(configuration(**overrides), client), client


def test_fetch_normalizes_market_prices_and_source() -> None:
    provider, client = importer(
        httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json=payload())
                if request.url.path == "/v1/marketdata"
                else httpx.Response(404)
            )
        )
    )
    try:
        data = provider.fetch(START, now=NOW)
    finally:
        client.close()

    assert data.timestamps == (START + timedelta(hours=1),)
    assert data.import_price_eur_per_kwh == (0.12,)
    assert data.export_price_eur_per_kwh == (0.12,)
    assert data.unit == "EUR/kWh"
    assert data.source.provider == "awattar.de"
    assert data.source.entity_id == "de"
    assert data.expires_at == START + timedelta(hours=2)


def test_fetch_keeps_all_intervals_when_retrieval_is_before_first_hour() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload())),
    )
    try:
        data = provider.fetch(START, now=START)
    finally:
        client.close()

    assert data.timestamps == (START, START + timedelta(hours=1))
    assert data.import_price_eur_per_kwh == (0.1, 0.12)


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"data": []},
        {"data": [{"start_timestamp": 0}]},
        payload(unit="EUR/kWh"),
        payload(prices=(200_000.0,)),
    ],
)
def test_fetch_rejects_empty_malformed_unsupported_or_out_of_range_data(
    bad_payload: dict[str, object],
) -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=bad_payload))
    )
    try:
        with pytest.raises(AwattarError):
            provider.fetch(START, now=START)
    finally:
        client.close()


def test_fetch_rejects_gaps_and_transport_failures() -> None:
    gap = payload()
    assert isinstance(gap["data"], list)
    gap["data"][1]["start_timestamp"] += 3_600_000
    gap["data"][1]["end_timestamp"] += 3_600_000
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=gap))
    )
    try:
        with pytest.raises(AwattarError, match="contiguous"):
            provider.fetch(START, now=START)
    finally:
        client.close()

    provider, client = importer(httpx.MockTransport(lambda _: httpx.Response(503)))
    try:
        with pytest.raises(AwattarError, match="HTTP 503"):
            provider.fetch(START, now=START)
    finally:
        client.close()


def test_freshness_checks_expiry_and_retrieval_age() -> None:
    provider, client = importer(
        httpx.MockTransport(lambda _: httpx.Response(200, json=payload())),
        max_data_age_seconds=7200,
    )
    try:
        data = provider.fetch(START, now=NOW)
    finally:
        client.close()

    assert provider.is_fresh(data, now=START + timedelta(hours=1, minutes=30))
    assert not provider.is_fresh(data, now=START + timedelta(hours=2))
    provider.configuration = configuration(max_data_age_seconds=1800)
    assert not provider.is_fresh(data, now=START + timedelta(hours=1, minutes=1))
