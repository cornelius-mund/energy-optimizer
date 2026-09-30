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
    *,
    prices: tuple[float, ...] = (100.0, 120.0),
    unit: str = "Eur/MWh",
    first: datetime = START,
) -> dict[str, object]:
    return {
        "data": [
            {
                "start_timestamp": int(
                    (first + timedelta(hours=index)).timestamp() * 1000
                ),
                "end_timestamp": int(
                    (first + timedelta(hours=index + 1)).timestamp() * 1000
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
    assert data.export_price_eur_per_kwh == data.import_price_eur_per_kwh


def epoch_milliseconds(value: datetime) -> str:
    return str(int(value.timestamp() * 1000))


def test_fetch_requests_the_explicit_price_window() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload())

    provider, client = importer(httpx.MockTransport(handler))
    try:
        provider.fetch(START, now=NOW)
        explicit_end = START + timedelta(hours=6)
        provider.fetch(START, explicit_end, now=NOW)
    finally:
        client.close()

    default_window, explicit_window = (dict(r.url.params) for r in requests)
    assert default_window == {
        "start": epoch_milliseconds(START),
        "end": epoch_milliseconds(START + timedelta(hours=48)),
    }
    assert explicit_window == {
        "start": epoch_milliseconds(START),
        "end": epoch_milliseconds(explicit_end),
    }


def test_fetch_validates_the_window_before_sending_a_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload())

    provider, client = importer(httpx.MockTransport(handler))
    try:
        with pytest.raises(AwattarError, match="aligned to the hour"):
            provider.fetch(START + timedelta(minutes=30), now=NOW)
        with pytest.raises(AwattarError, match="timezone"):
            provider.fetch(datetime(2026, 1, 1), now=NOW)
    finally:
        client.close()

    assert requests == []


def test_fetch_keeps_prices_that_reach_into_the_next_day() -> None:
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload(prices=tuple(range(36))))
        )
    )
    try:
        data = provider.fetch(START, now=START - timedelta(minutes=30))
    finally:
        client.close()

    assert len(data.timestamps) == 36
    assert data.timestamps[0] == START
    assert data.timestamps[-1] == START + timedelta(hours=35)
    assert data.expires_at == START + timedelta(hours=36)


def test_fetch_accepts_a_response_that_ends_at_the_local_end_of_today() -> None:
    first = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
    now = datetime(2026, 9, 30, 9, 56, tzinfo=timezone.utc)
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=payload(prices=tuple(range(13)), first=first)
            )
        )
    )
    try:
        data = provider.fetch(first, now=now)
    finally:
        client.close()

    assert data.timestamps[0] == datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    assert data.timestamps[-1] == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)
    assert data.expires_at == datetime(2026, 9, 30, 22, tzinfo=timezone.utc)


def test_fetch_ignores_intervals_at_or_after_the_window_end() -> None:
    provider, client = importer(
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload(prices=(100.0, 120.0, 140.0)))
        )
    )
    try:
        data = provider.fetch(START, START + timedelta(hours=2), now=START)
    finally:
        client.close()

    assert data.timestamps == (START, START + timedelta(hours=1))
    assert data.expires_at == START + timedelta(hours=2)


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
